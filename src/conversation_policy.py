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
    "memory_min_messages": 15,
    "memory_body_max_chars": 2000,
    "memory_fallback_sec": 21600,
    "memory_tail_sec": 600,
    "mention_per_minute": 3,
    "send_delay_min_sec": 1,
    "send_delay_max_sec": 3,
    "proactive_min_messages": 8,
    "proactive_min_participants": 2,
    "proactive_min_age_sec": 45,
    "proactive_min_new_messages": 10,
    "proactive_cooldown_sec": 3600,
    "proactive_max_replies": 2,
    "proactive_phases": "rising_peak",
    "proactive_daily_limit": 6,
    "proactive_eval_sec": 300,
    "proactive_context_count": 40,
    "proactive_max_chars": 80,
    "proactive_quiet_start": 0,
    "proactive_quiet_end": 8,
    "proactive_feedback_sec": 600,
    "proactive_feedback_messages": 5,
    "proactive_ignored_limit": 3,
    "proactive_allowed_moves": "追问、带细节的回应",
    "proactive_trigger_words": "觉得、认为、怎么看、为什么、喜欢、好吃、分享、推荐、猫、狗、宠物、美食、？、?",
    "proactive_resume_gap_sec": 120,
}

LIMITS = {
    "proactive_resume_gap_sec": (30, 3600),
    "proactive_daily_limit": (0, 100),
    "proactive_eval_sec": (30, 7200),
    "proactive_context_count": (10, 100),
    "proactive_max_chars": (20, 160),
    "proactive_quiet_start": (0, 23),
    "proactive_quiet_end": (0, 23),
    "proactive_feedback_sec": (60, 3600),
    "proactive_feedback_messages": (1, 30),
    "proactive_ignored_limit": (1, 10),
    "episode_gap_sec": (60, 3600),
    "episode_max_messages": (20, 100),
    "topic_min_messages": (3, 50),
    "topic_similarity": (0.0, 0.8),
    "memory_settle_sec": (60, 3600),
    "memory_min_messages": (2, 100),
    "memory_body_max_chars": (500, 10000),
    "memory_fallback_sec": (3600, 86400),
    "memory_tail_sec": (60, 1800),
    "mention_per_minute": (1, 60),
    "send_delay_min_sec": (0, 10),
    "send_delay_max_sec": (0, 10),
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
        if key in ("proactive_allowed_moves", "proactive_trigger_words"):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("允许的插话方式需为 1–200 字文本")
        elif key == "proactive_phases":
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
    if result["send_delay_min_sec"] > result["send_delay_max_sec"]:
        raise ValueError("发送延迟下限不能超过上限")
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
