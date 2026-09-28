"""ProactiveGate — decides whether the bot should evaluate speaking.

Combines message rate tracking, mode lookup, per-mode evaluation
intervals, and probabilistic gating.  Pure heuristic — zero AI cost
for the 99% of messages that get filtered out before reaching the AI.
"""

import logging
import random
import threading
import time
from typing import TYPE_CHECKING

from .modes import lookup_mode, ProactiveMode
from .rate_tracker import RateTracker

if TYPE_CHECKING:
    from ..config import BotConfig
    from ..db.store import MessageStore

logger = logging.getLogger(__name__)


class ProactiveGate:
    """Multi-level gate for proactive chat participation.

    On every message (without @mention), the gate:
    1. Records the message for rate tracking
    2. Computes current message rate
    3. Looks up the corresponding mode
    4. Checks if the evaluation interval has elapsed
    5. Rolls the dice against the mode's reply probability

    Only when ALL gates pass does it return a mode for the handler
    to call the AI with.  No daily limit, no hard cooldown — the
    per-mode interval + probability provides natural pacing.
    """

    def __init__(self, config: "BotConfig", store=None):
        self._config = config
        self._store = store
        self._lock = threading.RLock()
        self._tracker = RateTracker(config.proactive_rate_window_sec)
        # Per-group: last time we evaluated (to enforce eval_interval)
        self._last_eval: dict[str, float] = {}
        # Per-group: consecutive AI silence count (for exponential backoff)
        self._consecutive_silence: dict[str, int] = {}
        self._call_count: int = 0
        self._episodes: dict[str, dict] = {}

    def should_speak(self, msg: dict) -> tuple[bool, ProactiveMode | None, str]:
        with self._lock:
            return self._should_speak_unlocked(msg)

    def _should_speak_unlocked(self, msg: dict) -> tuple[bool, ProactiveMode | None, str]:
        """Record a message and decide whether to trigger AI evaluation.

        Args:
            msg: Standardized message dict (must have 'chat_id').

        Returns:
            (should_evaluate, mode, reason) — if should_evaluate is False,
            mode is None and reason explains which gate blocked.
        """
        chat_id = msg.get("chat_id", "")
        if not chat_id:
            return False, None, "no chat_id"

        # ── Periodic _last_eval cleanup ─────────────────────────────
        self._call_count += 1
        if self._call_count % 500 == 0:
            cutoff = time.time() - 3600  # 1 hour
            stale = [k for k, v in self._last_eval.items() if v < cutoff]
            for k in stale:
                del self._last_eval[k]
                self._consecutive_silence.pop(k, None)
            for k, state in list(self._episodes.items()):
                if state.get("last_seen", 0) < cutoff and not state.get("pending"):
                    del self._episodes[k]

        # ── Gate 1: master switch ─────────────────────────────────
        if not self._config.proactive_enabled:
            return False, None, "disabled"

        self._tracker.record(chat_id)
        if self._store is not None:
            allowed, reason = self._episode_gate(msg)
            if not allowed:
                return False, None, reason

        # ── Gate 2: message rate ──────────────────────────────────
        rate = self._tracker.rate(chat_id)
        mode = lookup_mode(rate, self._config)

        if mode.name == "SLEEP":
            logger.debug(
                "Proactive: rate=%.1f/min → SLEEP (chat=%s)",
                rate, chat_id[:20],
            )
            return False, None, f"rate {rate:.1f}/min → SLEEP"

        # ── Gate 3: evaluation interval (with silence backoff) ───
        now = time.time()
        last = self._last_eval.get(chat_id, 0)
        elapsed = now - last
        # Exponential backoff: each consecutive silence doubles the
        # effective interval, capped at 16x.  During prolonged crises
        # this prevents burning API tokens every 2-8 minutes on calls
        # that all return empty.
        consecutive = self._consecutive_silence.get(chat_id, 0)
        backoff = min(2 ** consecutive, 16)
        effective_interval = mode.eval_interval_sec * backoff
        if elapsed < effective_interval:
            logger.debug(
                "Proactive: eval interval not met (%.0fs < %ds, mode=%s, "
                "backoff=%dx, consecutive_silence=%d)",
                elapsed, effective_interval, mode.name,
                backoff, consecutive,
            )
            return False, None, (
                f"eval interval ({elapsed:.0f}s < {effective_interval}s, "
                f"backoff={backoff}x)"
            )

        # ── Gate 4: reply probability ─────────────────────────────
        roll = random.random()
        if roll > mode.reply_probability:
            logger.debug(
                "Proactive: probability miss (%.2f > %.2f, mode=%s)",
                roll, mode.reply_probability, mode.name,
            )
            # Update last_eval so we don't hammer the probability gate
            self._last_eval[chat_id] = now
            return False, None, f"probability miss ({roll:.2f} > {mode.reply_probability})"

        # ── All gates passed ──────────────────────────────────────
        self._last_eval[chat_id] = now
        if self._store is not None:
            self._episodes[chat_id]["pending"] = True
        logger.info(
            "Proactive: GATE PASSED mode=%s rate=%.1f/min "
            "interval=%ds prob=%.0f%% (chat=%s)",
            mode.name, rate, mode.eval_interval_sec,
            mode.reply_probability * 100, chat_id[:20],
        )
        return True, mode, f"mode={mode.name} rate={rate:.1f}/min"

    def _episode_gate(self, msg: dict) -> tuple[bool, str]:
        from src.conversation_episodes import split_episodes
        from src.conversation_policy import load_policy

        policy = load_policy()
        chat_id = msg["chat_id"]
        now = time.time()
        if now - msg.get("timestamp", 0) > 120:
            return False, "stale message"
        rows = self._store.get_recent_messages(
            chat_id, msg.get("timestamp", int(now)), limit=500,
        )
        episodes = split_episodes(rows, policy, now=now)
        if not episodes:
            return False, "no conversation"
        episode = episodes[-1]
        key = episode["start_id"]
        state = self._episodes.get(chat_id)
        if state is None or state["key"] != key:
            state = {"key": key, "replies": 0, "last_speech_at": 0,
                     "last_speech_count": 0, "pending": False}
            self._episodes[chat_id] = state
        state["context"] = episode["rows"]
        state["last_seen"] = now
        timestamps = [item["timestamp"] for item in episode["rows"]]
        current = sum(t >= now - 60 for t in timestamps)
        previous = sum(now - 120 <= t < now - 60 for t in timestamps)
        phase = ("rising" if current >= previous + 2 else
                 "falling" if previous >= 2 and current * 2 < previous else "peak")
        state["phase"] = phase
        if state["pending"]:
            return False, "AI evaluation already running"
        if episode["count"] < policy["proactive_min_messages"]:
            return False, "episode too short"
        if episode["participants"] < policy["proactive_min_participants"]:
            return False, "too few participants"
        if timestamps[-1] - timestamps[0] < policy["proactive_min_age_sec"]:
            return False, "episode too young"
        if state["replies"] >= policy["proactive_max_replies"]:
            return False, "episode reply budget reached"
        if state["last_speech_at"]:
            if now - state["last_speech_at"] < policy["proactive_cooldown_sec"]:
                return False, "episode cooldown"
            if episode["count"] - state["last_speech_count"] < policy["proactive_min_new_messages"]:
                return False, "not enough new turns"
        if policy["proactive_phases"] == "rising_peak" and phase == "falling":
            return False, "conversation cooling"
        return True, f"episode {phase}"

    def episode_context(self, chat_id: str, limit: int) -> tuple[list[dict], str]:
        with self._lock:
            state = self._episodes.get(chat_id)
            if not state:
                return [], ""
            return list(state.get("context", [])[-limit:]), state.get("phase", "")

    def record_eval(self, chat_id: str) -> None:
        """Manually update last evaluation time (e.g., after AI returned blank)."""
        self._last_eval[chat_id] = time.time()

    def record_silence(self, chat_id: str) -> None:
        """Increment consecutive silence counter for exponential backoff.

        Call this when the AI returns an empty string from proactive_chat().
        Each consecutive silence doubles the effective evaluation interval,
        capped at 16x, so the gate backs off during prolonged crises.
        """
        with self._lock:
            self._consecutive_silence[chat_id] = (
                self._consecutive_silence.get(chat_id, 0) + 1
            )
            if chat_id in self._episodes:
                self._episodes[chat_id]["pending"] = False
        logger.debug(
            "Proactive: silence recorded for chat=%s (consecutive=%d)",
            chat_id[:20], self._consecutive_silence[chat_id],
        )

    def record_speech(self, chat_id: str) -> None:
        """Reset consecutive silence counter when the AI successfully speaks."""
        with self._lock:
            state = self._episodes.get(chat_id)
            if state:
                state["pending"] = False
                state["replies"] += 1
                state["last_speech_at"] = time.time()
                state["last_speech_count"] = len(state.get("context", []))
        if self._consecutive_silence.get(chat_id, 0) > 0:
            logger.debug(
                "Proactive: speech recorded for chat=%s — resetting "
                "silence counter from %d",
                chat_id[:20], self._consecutive_silence[chat_id],
            )
            self._consecutive_silence[chat_id] = 0

    def record_failure(self, chat_id: str) -> None:
        """Release a reserved evaluation after an API error."""
        with self._lock:
            if chat_id in self._episodes:
                self._episodes[chat_id]["pending"] = False

    def get_consecutive_silence(self, chat_id: str) -> int:
        """Return the current consecutive silence count for a group."""
        return self._consecutive_silence.get(chat_id, 0)
