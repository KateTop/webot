"""Editable, bounded controls for conversation episodes and memory batching."""

import json
import os
import threading
from pathlib import Path

from src.config import PROJECT_ROOT

POLICY_FILE = PROJECT_ROOT / "data" / "conversation_policy.json"

DEFAULT_POLICY = {
    "episode_gap_sec": 900,
    "episode_max_messages": 100,
    "topic_min_messages": 8,
    "topic_similarity": 0.12,
    "memory_settle_sec": 600,
    "memory_min_messages": 12,
    "proactive_min_messages": 8,
    "proactive_min_participants": 2,
    "proactive_min_age_sec": 45,
    "proactive_min_new_messages": 10,
    "proactive_cooldown_sec": 300,
    "proactive_max_replies": 2,
    "proactive_phases": "rising_peak",
}

LIMITS = {
    "episode_gap_sec": (60, 3600),
    "episode_max_messages": (20, 100),
    "topic_min_messages": (3, 50),
    "topic_similarity": (0.0, 0.8),
    "memory_settle_sec": (60, 3600),
    "memory_min_messages": (2, 100),
    "proactive_min_messages": (2, 100),
    "proactive_min_participants": (1, 20),
    "proactive_min_age_sec": (0, 600),
    "proactive_min_new_messages": (1, 100),
    "proactive_cooldown_sec": (30, 7200),
    "proactive_max_replies": (0, 10),
}

_lock = threading.RLock()
_cached_stamp = None
_cached_policy = None


def _validate(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("对话策略必须是对象")
    result = dict(DEFAULT_POLICY)
    for key, value in values.items():
        if key not in result:
            continue
        if key == "proactive_phases":
            if value not in ("rising_peak", "all_active"):
                raise ValueError("主动发言阶段无效")
        else:
            lower, upper = LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} 必须是数字")
            if not lower <= value <= upper:
                raise ValueError(f"{key} 必须在 {lower}–{upper} 之间")
            if key != "topic_similarity" and int(value) != value:
                raise ValueError(f"{key} 必须是整数")
            value = float(value) if key == "topic_similarity" else int(value)
        result[key] = value
    return result


def load_policy() -> dict:
    """Reload after external edits while avoiding a file read per message."""
    global _cached_stamp, _cached_policy
    with _lock:
        try:
            stamp = (str(POLICY_FILE), POLICY_FILE.stat().st_mtime_ns)
        except FileNotFoundError:
            return dict(DEFAULT_POLICY)
        if stamp == _cached_stamp and _cached_policy is not None:
            return dict(_cached_policy)
        try:
            policy = _validate(json.loads(POLICY_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            policy = dict(DEFAULT_POLICY)
        _cached_stamp, _cached_policy = stamp, policy
        return dict(policy)


def save_policy(values: dict) -> dict:
    global _cached_stamp, _cached_policy
    with _lock:
        result = _validate({**load_policy(), **values})
        POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = POLICY_FILE.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, POLICY_FILE)
        finally:
            tmp.unlink(missing_ok=True)
        _cached_stamp = (str(POLICY_FILE), POLICY_FILE.stat().st_mtime_ns)
        _cached_policy = result
        return dict(result)
