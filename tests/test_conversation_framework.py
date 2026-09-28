"""Synthetic episode and policy checks; never open a real chat database."""

import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.conversation_episodes import split_episodes
from src.conversation_policy import DEFAULT_POLICY
from src.db import MessageStore, initialize_db
from src.memory.consolidator import MemoryConsolidator
from src.proactive.gate import ProactiveGate


def _row(number, timestamp, content, sender="a"):
    return {"id": number, "message_id": f"m{number}", "chat_id": "g",
            "sender_id": sender, "sender_name": sender,
            "content": content, "timestamp": timestamp}


def test_gap_and_confirmed_topic_change_are_distinct():
    policy = dict(DEFAULT_POLICY, topic_min_messages=3)
    rows = [_row(i, 100 + i * 10, "猫咪每天喜欢晒太阳", "a" if i % 2 else "b")
            for i in range(4)]
    rows += [_row(4, 150, "火车车票需要提前预订"),
             _row(5, 160, "火车车票今天可以预订"),
             _row(6, 2000, "晚上吃什么")]
    segments = split_episodes(rows, policy, now=2100)
    assert [part["count"] for part in segments] == [4, 2, 1]
    assert [part["end_reason"] for part in segments[:2]] == ["话题变化", "间隔"]


def test_auto_memory_waits_for_settled_batch_and_file_creation(tmp_path, monkeypatch):
    from src import conversation_policy
    monkeypatch.setattr(conversation_policy, "POLICY_FILE", tmp_path / "policy.json")
    conversation_policy.save_policy({"memory_min_messages": 3, "memory_settle_sec": 60})
    conn = initialize_db(str(tmp_path / "messages.db"))
    try:
        store = MessageStore(conn)
        for number in range(3):
            store.insert_message(_row(number, int(time.time()) - 10 + number,
                                      "synthetic conversation"))
        summarizer = Mock()
        summarizer.consolidate_memory.return_value = "settled soul"
        worker = MemoryConsolidator(store, summarizer)
        assert worker.check_and_consolidate("g") is False
        summarizer.consolidate_memory.assert_not_called()
        path = store.ensure_group_memory_file("g")
        assert path.exists() and path.read_text(encoding="utf-8") == ""
        assert store.get_group_memory("g")["last_message_id"] is None
        store.insert_message(_row(3, int(time.time()) + 1000, "new episode"))
        assert worker.check_and_consolidate("g") is True
        assert store.get_group_memory("g")["last_message_id"] == "m2"
        assert store.get_group_memory("g")["memory_text"] == "settled soul"
    finally:
        conn.close()


def test_proactive_requires_episode_and_reserves_one_ai_attempt():
    now = int(time.time())
    rows = [_row(i, now - 90 + i * 10, "我们正在聊今天的电影", "a" if i % 2 else "b")
            for i in range(10)]
    store = SimpleNamespace(get_recent_messages=lambda *args, **kwargs: list(rows))
    config = SimpleNamespace(proactive_enabled=True, proactive_rate_window_sec=120,
                             proactive_rate_quiet=1.5, proactive_rate_casual=4.0,
                             proactive_rate_lively=6.5, proactive_rate_burst=8.5)
    gate = ProactiveGate(config, store)
    gate._tracker = SimpleNamespace(record=lambda _: None, rate=lambda _: 5.0)
    with patch("src.proactive.gate.random.random", return_value=0):
        allowed, mode, _ = gate.should_speak(rows[-1])
        assert allowed and mode.name == "CASUAL"
        assert len(gate.episode_context("g", 30)[0]) == 10
        assert gate.should_speak(rows[-1])[0] is False
        gate.record_speech("g")
        assert gate.should_speak(rows[-1])[0] is False


def test_policy_rejects_out_of_range_values(tmp_path, monkeypatch):
    from src import conversation_policy
    monkeypatch.setattr(conversation_policy, "POLICY_FILE", tmp_path / "policy.json")
    with pytest.raises(ValueError):
        conversation_policy.save_policy({"proactive_max_replies": -1})
    saved = conversation_policy.save_policy({"episode_gap_sec": 300})
    assert saved["episode_gap_sec"] == 300
    assert conversation_policy.load_policy()["episode_gap_sec"] == 300
