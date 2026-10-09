"""Persistent, editable instructions appended to built-in AI prompts."""

import json
import logging
import os
import threading
from pathlib import Path

from src.config import PROJECT_ROOT

PROMPT_FILE = PROJECT_ROOT / "data" / "prompts.json"
PROMPT_FIELDS = ("chat", "summary", "memory", "persona", "proactive", "base", "settle")
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def load_prompt_settings() -> dict[str, str]:
    """Read the current instructions; edits take effect on the next AI call."""
    with _lock:
        if not PROMPT_FILE.exists():
            return {key: "" for key in PROMPT_FIELDS}
        try:
            data = json.loads(PROMPT_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not load prompt settings; using defaults")
            return {key: "" for key in PROMPT_FIELDS}
        if not isinstance(data, dict):
            return {key: "" for key in PROMPT_FIELDS}
    return {key: data.get(key, "") if isinstance(data.get(key, ""), str) else ""
            for key in PROMPT_FIELDS}


def save_prompt_settings(data: dict) -> dict[str, str]:
    """Validate and atomically persist user instructions."""
    if not isinstance(data, dict):
        raise ValueError("Prompt 配置必须是对象")
    current = load_prompt_settings()
    result = dict(current)
    for key, value in data.items():
        if key not in PROMPT_FIELDS:
            continue
        if not isinstance(value, str) or len(value) > 12000:
            raise ValueError(f"{key} 必须是 12000 字以内的文本")
        result[key] = value
    with _lock:
        PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROMPT_FILE.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, PROMPT_FILE)
    return result


PLACEHOLDERS = ("name", "base", "soul", "today", "history", "recent", "max_chars", "persona", "my_recent", "allowed_moves", "sender", "question")


def render_template(template: str, values: dict) -> str:
    """One-pass replacement of reserved tokens only; JSON and inserted text stay literal."""
    import re
    pattern = r"(?<!\{)\{(" + "|".join(PLACEHOLDERS) + r")\}(?!\})"
    return re.sub(pattern, lambda match: str(values.get(match[1], "（本场景未提供）")), template)


def format_history(rows):
    import datetime
    lines = []
    for row in rows or []:
        stamp = row.get("timestamp", 0)
        when = datetime.datetime.fromtimestamp(stamp).isoformat(timespec="minutes") if stamp else "时间未知"
        lines.append(f"[{when}] {row.get('sender_name', '?')} ({row.get('sender_id', '?')}) message_id={row.get('message_id', row.get('id', '?'))}: {row.get('content', '')}")
    return "\n".join(lines) or "（无可用聊天记录）"


def template_values(values=None):
    import datetime
    from src.conversation_policy import load_policy
    settings = load_prompt_settings()
    policy = load_policy()
    result = {key: "（本场景未提供）" for key in PLACEHOLDERS}
    result.update(name="群聊小助手", base="（基础人设中不展开自身）", today=datetime.date.today().isoformat(),
        persona=settings.get("persona", "") or "好奇，重视具体细节，不喜欢空话；不懂就说不懂",
        max_chars=policy["memory_body_max_chars"], allowed_moves=policy["proactive_allowed_moves"])
    result.update(values or {})
    return result


def with_user_instructions(default_prompt: str, field: str, values=None) -> str:
    """Expand user templates, preserving all non-reserved braces."""
    custom = load_prompt_settings().get(field, "").strip()
    context = template_values(values)
    from src.memory.instructions import base_prompt
    context["base"] = base_prompt(context["name"], context)
    return default_prompt + ("\n\n## 用户自定义指令\n" + render_template(custom, context) if custom else "")
