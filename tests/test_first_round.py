"""Regression coverage for configuration, prompts, search, and group isolation."""

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.db.store import MessageStore
from src.router import MessageRouter
from src.summarize.openai_backend import OpenAISummarizer
from src.summarize import prompt_settings
from src.web.server import _update_env


def test_env_updates_preserve_secrets_and_concurrent_changes(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=real-test-value\nCUSTOM_OPTION=keep\n", encoding="utf-8")
    threads = [threading.Thread(target=_update_env, args=(env, {f"OPTION_{i}": str(i)}))
               for i in range(20)]
    threads.append(threading.Thread(target=_update_env, args=(env, {"OPENAI_API_KEY": "real***alue"})))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    saved = env.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=real-test-value" in saved
    assert "CUSTOM_OPTION=keep" in saved
    assert all(f"OPTION_{i}={i}" in saved for i in range(20))


def test_prompt_settings_persist_and_validate(tmp_path):
    with patch.object(prompt_settings, "PROMPT_FILE", tmp_path / "prompts.json"):
        assert prompt_settings.save_prompt_settings({"chat": "回答简短", "summary": "列出待办"}) == {
            "chat": "回答简短", "summary": "列出待办"}
        assert prompt_settings.load_prompt_settings()["chat"] == "回答简短"
        assert "回答简短" in prompt_settings.with_user_instructions("默认", "chat")
        with pytest.raises(ValueError):
            prompt_settings.save_prompt_settings({"chat": "x" * 12001})


def test_search_is_limited_to_current_group():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE messages (message_id TEXT, chat_id TEXT, sender_id TEXT,
                 sender_name TEXT, content TEXT, msg_type INTEGER, timestamp INTEGER)""")
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("1", "group-a", "a", "甲", "项目进度", 1, 100),
        ("2", "group-b", "b", "乙", "项目进度", 1, 101),
    ])
    found = MessageStore(conn).search_group_messages("group-a", "项目", 0)
    assert [message["message_id"] for message in found] == ["1"]


def test_query_excludes_its_own_trigger_message():
    seen = {}
    class Store:
        def get_messages_since(self, *args, **kwargs):
            return []
        def search_group_messages(self, chat_id, keyword, since_ts):
            seen["chat_id"] = chat_id
            return [{"message_id": "trigger", "sender_id": "user", "sender_name": "用户",
                     "content": "搜索群聊 项目", "timestamp": 100}]
    router = SimpleNamespace(
        _store=Store(),
        _nicks=SimpleNamespace(resolve_name=lambda sender: sender),
        _config=SimpleNamespace(bot_display_name="机器人"),
    )
    reply = MessageRouter._handle_chat(router, {
        "message_id": "trigger", "chat_id": "group-a", "sender_id": "user",
        "sender_name": "用户"}, "搜索群聊 项目")
    assert seen["chat_id"] == "group-a"
    assert "没有找到" in reply


def test_web_search_tool_is_opt_in():
    calls = []
    class Completion:
        def create(self, **kwargs):
            calls.append(kwargs)
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]})()
    summarizer = OpenAISummarizer.__new__(OpenAISummarizer)
    summarizer.client = type("Client", (), {"chat": type("Chat", (), {"completions": Completion()})()})()
    summarizer.model = "test-model"
    summarizer.web_search = True
    assert summarizer._call_chat_api("system", [{"role": "user", "content": "hello"}]) == "ok"
    assert calls[-1]["tools"] == [{"type": "web_search"}]
    summarizer.web_search = False
    summarizer._call_chat_api("system", [])
    assert "tools" not in calls[-1]
