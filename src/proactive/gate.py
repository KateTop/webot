"""Rule-filtered speaking opportunities with persistent group pacing and feedback."""

import logging
import random
import threading
import time
from datetime import datetime
from dataclasses import replace
from typing import TYPE_CHECKING

from .modes import lookup_mode, ProactiveMode
from .rate_tracker import RateTracker

if TYPE_CHECKING:
    from ..config import BotConfig
    from ..db.store import MessageStore

logger = logging.getLogger(__name__)


class ProactiveGate:
    """Filter candidates; the AI must still find a useful contribution or SKIP."""

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
        self._participation: dict[str, dict] = {}

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

        if self._store is not None:
            from src.conversation_policy import load_policy
            policy = load_policy()
            now = time.time()
            if now - self._last_eval.get(chat_id, 0) < policy["proactive_eval_sec"] * 2 ** min(self._consecutive_silence.get(chat_id, 0), 4):
                return False, None, "evaluation backoff"
            # Activity describes the atmosphere, never increases the chance of interrupting.
            mode = replace(mode, max_chars=policy["proactive_max_chars"],
                           context_count=policy["proactive_context_count"])
        else:
            if mode.name == "SLEEP":
                return False, None, "sleep"
            now = time.time()
            if now - self._last_eval.get(chat_id, 0) < mode.eval_interval_sec:
                return False, None, "evaluation interval"
            if random.random() > mode.reply_probability:
                self._last_eval[chat_id] = now
                return False, None, "probability miss"

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
        allowed, reason = self._participation_gate(msg, policy, episode)
        if not allowed:
            return False, reason
        if state["last_speech_at"]:
            if episode["count"] - state["last_speech_count"] < policy["proactive_min_new_messages"]:
                return False, "not enough new turns"
        if policy["proactive_phases"] == "rising_peak" and phase == "falling":
            return False, "conversation cooling"
        return True, f"episode {phase}"

    def _group_state(self, chat_id):
        if chat_id not in self._participation:
            loader = getattr(self._store, "get_proactive_state", None)
            self._participation[chat_id] = loader(chat_id) if callable(loader) else {}
        return self._participation[chat_id]

    def _save_group(self, chat_id):
        saver = getattr(self._store, "save_proactive_state", None)
        if callable(saver):
            saver(chat_id, self._group_state(chat_id))

    def observe(self, msg):
        """Only an explicit @ or quote is evidence of engagement, never proximity."""
        from src.conversation_policy import load_policy
        with self._lock:
            state = self._group_state(msg["chat_id"])
            policy = load_policy()
            pending = state.get("feedback")
            if not pending or msg.get("timestamp", 0) <= pending["at"]:
                return
            if msg.get("is_at_mentioned") or msg.get("quotes_bot"):
                state["ignored"] = 0
                state["feedback"] = None
                state["reaction"] = "有人艾特或引用回应；不能证明针对哪次插话"
            else:
                pending["seen"] += 1
                if pending["seen"] >= policy["proactive_feedback_messages"] or time.time() - pending["at"] >= policy["proactive_feedback_sec"]:
                    state["ignored"] = state.get("ignored", 0) + 1
                    state["feedback"] = None
                    state["reaction"] = "未观察到明确接话（不等于不喜欢）"
            self._save_group(msg["chat_id"])

    def record_direct_reply(self, chat_id):
        with self._lock:
            self._group_state(chat_id)["last_speech"] = time.time()
            self._save_group(chat_id)

    def participation_context(self, chat_id):
        with self._lock:
            state = self._group_state(chat_id)
            return state.get("reaction", "暂无反馈"), self._episodes.get(chat_id, {}).get("reason", "")

    def _participation_gate(self, msg, policy, episode):
        now = time.time()
        hour = datetime.fromtimestamp(now).hour
        start, end = policy["proactive_quiet_start"], policy["proactive_quiet_end"]
        quiet = start <= hour < end if start < end else (hour >= start or hour < end) if start != end else False
        if quiet:
            return False, "quiet hours"
        state = self._group_state(msg["chat_id"])
        today = datetime.fromtimestamp(now).date().isoformat()
        if state.get("day") != today:
            state.update(day=today, count=0)
        if state.get("count", 0) >= policy["proactive_daily_limit"]:
            return False, "daily budget reached"
        factor = 2 if state.get("ignored", 0) >= policy["proactive_ignored_limit"] else 1
        if now - state.get("last_speech", 0) < policy["proactive_cooldown_sec"] * factor:
            return False, "group cooldown"
        rows = episode["rows"]
        effective = [r for r in rows[-policy["proactive_context_count"]:] if len(str(r.get("content", "")).strip()) > 3]
        if len(effective) < policy["proactive_min_messages"]:
            return False, "too few meaningful messages"
        text = str(msg.get("content", ""))
        # Cheap candidate signals; the AI still decides whether there is anything to add.
        if any(word.strip() in text for word in policy["proactive_trigger_words"].split("、") if word.strip()):
            reason = "当前有观点、分享或开放问题"
        elif len(rows) > 1 and rows[-1]["timestamp"] - rows[-2]["timestamp"] >= policy["proactive_resume_gap_sec"]:
            reason = "停顿后有人重新展开话题"
        else:
            return False, "no concrete reason to interject"
        self._episodes[msg["chat_id"]]["reason"] = reason
        return True, reason

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
        """Reserve budget for a generated reply; this is not proof of delivery."""
        with self._lock:
            group = self._group_state(chat_id)
            now = time.time()
            day = datetime.fromtimestamp(now).date().isoformat()
            if group.get("day") != day:
                group.update(day=day, count=0)
            group["count"] = group.get("count", 0) + 1
            group["last_speech"] = now
            group["feedback"] = {"at": now, "seen": 0}
            self._save_group(chat_id)
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
