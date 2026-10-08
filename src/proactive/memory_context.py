"""Conservative memory projection for ambient speech; never rewrite soul.md."""

import re


def select_memory(text: str, messages: list[dict]) -> str:
    names = {str(m.get(key, "")) for m in messages for key in ("sender_id", "sender_name")}
    names.discard("")
    section = ""
    result = []
    for line in text.splitlines():
        if any(marker in line for marker in ("知道就好", "不主动提", "健康", "财务", "家庭矛盾")):
            continue
        heading = re.fullmatch(r"\s*【(.+)】\s*", line)
        if heading:
            section = heading[1]
            continue
        if section in ("群里的事", "我说过的立场") or (
            section == "每个人" and any(name in line for name in names)
        ):
            result.append(line)
    # Legacy unstructured memories are omitted rather than disclosing unrelated entries.
    return "\n".join(result)[:2000]
