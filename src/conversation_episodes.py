"""Deterministic per-group conversation boundaries; no API call per message."""

import re
import time


def _terms(text: str) -> set[str]:
    text = (text or "").lower()[:500]
    words = set(re.findall(r"[a-z][a-z0-9]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        words.update(run[index:index + 2] for index in range(len(run) - 1))
    return words


def _similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def split_episodes(messages: list[dict], policy: dict, now: float | None = None) -> list[dict]:
    """Split by idle gap, size and a confirmed lexical topic change.

    A topic change needs two related messages on the new topic so a single
    offhand remark does not fragment the conversation.
    """
    if not messages:
        return []
    now = time.time() if now is None else now
    gap = policy["episode_gap_sec"]
    maximum = policy["episode_max_messages"]
    min_topic = policy["topic_min_messages"]
    threshold = policy["topic_similarity"]
    result = []
    current = []
    current_reason = "开始"

    def finish(reason: str):
        if not current:
            return
        result.append({
            "rows": list(current),
            "start_id": current[0].get("id", current[0].get("message_id")),
            "end_id": current[-1].get("id", current[-1].get("message_id")),
            "start_time": current[0]["timestamp"],
            "end_time": current[-1]["timestamp"],
            "count": len(current),
            "start_reason": current_reason,
            "end_reason": reason,
            "preview": next((str(row.get("content", ""))[:40]
                             for row in current if row.get("content")), ""),
            "participants": len({row.get("sender_id") for row in current if row.get("sender_id")}),
        })

    for index, message in enumerate(messages):
        reason = None
        if current:
            delta = message["timestamp"] - current[-1]["timestamp"]
            if delta < -60:
                reason = "补录倒序"
            elif delta >= gap:
                reason = "间隔"
            elif len(current) >= maximum:
                reason = "条数"
            elif len(current) >= min_topic and index + 1 < len(messages):
                previous = set().union(*(_terms(row.get("content", "")) for row in current[-5:]))
                candidate = _terms(message.get("content", ""))
                following = messages[index + 1]
                next_terms = _terms(following.get("content", ""))
                if (len(candidate) >= 3 and len(next_terms) >= 3
                        and following["timestamp"] - message["timestamp"] < gap
                        and _similarity(previous, candidate) < threshold
                        and _similarity(candidate, next_terms) >= threshold):
                    reason = "话题变化"
        if reason:
            finish(reason)
            current = []
            current_reason = reason
        current.append(message)
    finish("进行中")
    for part in result:
        part["closed"] = (part is not result[-1]
                          or now - part["end_time"] >= policy["memory_settle_sec"])
        if part["closed"] and part["end_reason"] == "进行中":
            part["end_reason"] = "沉寂"
    return result
