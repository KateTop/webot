"""Memory editing and manual consolidation use the same persisted cursor."""

from unittest.mock import Mock

import pytest

from src.db import MessageStore, initialize_db
from src.memory.consolidator import MemoryConsolidator
from src.summarize import prompt_settings


def _message(number, when):
    return {
        "message_id": f"m{number}", "chat_id": "group@chatroom",
        "sender_id": "user", "sender_name": "User",
        "content": f"synthetic {number}", "timestamp": when,
    }


def test_soul_file_and_manual_segment_cursor(tmp_path):
    conn = initialize_db(str(tmp_path / "messages.db"))
    try:
        store = MessageStore(conn)
        for number, when in enumerate((100, 110, 1500, 1510)):
            store.insert_message(_message(number, when))
        pending = store.list_pending_memory_segments("group@chatroom")
        assert [item["count"] for item in pending["segments"]] == [2, 2]
        first, second = pending["segments"]
        assert [m["content"] for m in store.get_pending_segment_messages(
            "group@chatroom", first["start_id"], first["end_id"])] == [
                "synthetic 0", "synthetic 1"]

        store.save_group_memory_text("group@chatroom", "initial soul")
        path = store._soul_path("group@chatroom")
        assert path.name == "soul.md" and path.read_text(encoding="utf-8") == "initial soul"

        summarizer = Mock()
        summarizer.consolidate_memory.return_value = "updated soul"
        worker = MemoryConsolidator(store, summarizer)
        with pytest.raises(ValueError):
            worker.consolidate_first_pending_segment("group@chatroom", second["end_id"])
        assert worker.consolidate_first_pending_segment("group@chatroom", first["end_id"])
        assert store.get_group_memory("group@chatroom")["last_message_id"] == "m1"
        assert store.get_group_memory("group@chatroom")["memory_text"] == "updated soul"
        assert [item["count"] for item in store.list_pending_memory_segments(
            "group@chatroom")["segments"]] == [2]

        worker.save_manual_text("group@chatroom", "edited soul")
        assert store.get_group_memory("group@chatroom")["last_message_id"] == "m1"
        assert path.read_text(encoding="utf-8") == "edited soul"
        path.write_text("external edit", encoding="utf-8")
        assert store.get_group_memory("group@chatroom")["memory_text"] == "external edit"
    finally:
        conn.close()


def test_partial_prompt_save_preserves_memory_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_settings, "PROMPT_FILE", tmp_path / "prompts.json")
    prompt_settings.save_prompt_settings({"memory": "remember important events"})
    prompt_settings.save_prompt_settings({"chat": "answer briefly"})
    saved = prompt_settings.load_prompt_settings()
    assert saved["memory"] == "remember important events"
    assert saved["chat"] == "answer briefly"
