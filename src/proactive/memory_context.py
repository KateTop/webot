"""Conservative memory projection for ambient speech; never rewrite soul.md."""

import re


def select_memory(text: str, messages: list[dict]) -> str:
    from src.memory.document import parse
    names = {str(m.get("sender_id", "")) for m in messages}
    try:
        entries = parse(text)
    except ValueError:
        return ""
    lines = []
    for entry in entries:
        if entry["section"] == "观察中" or any(word in entry["text"] for word in ("不主动提", "知道就好", "健康", "财务", "家庭矛盾")):
            continue
        if entry["section"] in ("群里的事", "我说过的立场") or entry.get("subject_id") in names:
            lines.append(entry["text"])
    return "\n".join(lines)[:2000]
