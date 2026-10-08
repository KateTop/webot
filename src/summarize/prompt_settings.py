"""Persistent, editable instructions appended to built-in AI prompts."""

import json
import logging
import os
import threading
from pathlib import Path

from src.config import PROJECT_ROOT

PROMPT_FILE = PROJECT_ROOT / "data" / "prompts.json"
PROMPT_FIELDS = ("chat", "summary", "memory", "persona", "proactive")
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


def with_user_instructions(default_prompt: str, field: str) -> str:
    """Append configured instructions without formatting user supplied braces."""
    custom = load_prompt_settings().get(field, "").strip()
    return default_prompt + ("\n\n## 用户自定义指令\n" + custom if custom else "")
