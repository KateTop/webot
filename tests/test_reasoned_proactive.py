"""Synthetic tests: do not access user messages or send WeChat messages."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import conversation_policy
from src.db import MessageStore, initialize_db
from src.proactive.gate import ProactiveGate
from src.proactive.memory_context import select_memory
from src.summarize.openai_backend import OpenAISummarizer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(conversation_policy, "POLICY_FILE", tmp_path / "policy.json")
    conversation_policy.save_policy({"proactive_quiet_start": 0, "proactive_quiet_end": 0})
    conn = initialize_db(str(tmp_path / "test.db"))
    config = SimpleNamespace(proactive_enabled=True, proactive_rate_window_sec=120,
        proactive_rate_quiet=1.5, proactive_rate_casual=4, proactive_rate_lively=6.5,
        proactive_rate_burst=8.5)
    yield config, MessageStore(conn)
    conn.close()


def test_budget_and_cooldown_survive_restart_and_new_episode(setup):
    config, store = setup
    gate = ProactiveGate(config, store)
    gate.record_speech("synthetic")
    restarted = ProactiveGate(config, store)
    restarted._episodes["synthetic"] = {}
    policy = conversation_policy.load_policy()
    episode = {"rows": []}
    assert restarted._participation_gate({"chat_id": "synthetic"}, policy, episode)[1] == "group cooldown"
    policy["proactive_daily_limit"] = 1
    assert restarted._participation_gate({"chat_id": "synthetic"}, policy, episode)[1] == "daily budget reached"


def test_feedback_only_explicit_engagement_resets_backoff(setup):
    config, store = setup
    gate = ProactiveGate(config, store)
    with patch("src.proactive.gate.time.time", return_value=1000):
        gate.record_speech("synthetic")
        for number in range(5):
            gate.observe({"chat_id": "synthetic", "timestamp": 1001 + number})
        assert gate._group_state("synthetic")["ignored"] == 1
        gate.record_speech("synthetic")
        gate.observe({"chat_id": "synthetic", "timestamp": 1002, "quotes_bot": True})
        assert gate._group_state("synthetic")["ignored"] == 0


@pytest.mark.parametrize("answer", [" SKIP。 ", "**skip**", "x" * 81])
def test_skip_or_essay_never_becomes_message(setup, answer):
    provider = OpenAISummarizer.__new__(OpenAISummarizer)
    provider.max_retries = 1
    provider._call_chat_api = lambda *args: answer
    with patch("src.summarize.prompt_settings.load_prompt_settings", return_value={}):
        assert provider.proactive_chat(SimpleNamespace(), [{"content": "synthetic topic"}]) == ""


def test_memory_excludes_absent_people_and_sensitive_lines():
    memory = "【每个人】\nAlice: likes tea\nBob: likes coffee\nAlice: 知道就好，不主动提\n【群里的事】\nweekly picnic\n【观察中】\nunconfirmed\n【我说过的立场】\nprefer tea"
    selected = select_memory(memory, [{"sender_name": "Alice"}])
    assert "Alice: likes tea" in selected
    assert "Bob" not in selected and "不主动提" not in selected
    assert "unconfirmed" not in selected and "prefer tea" in selected


def test_no_reason_no_api_candidate_and_quiet_hours(setup):
    config, store = setup
    gate = ProactiveGate(config, store)
    gate._episodes["synthetic"] = {}
    import time
    now = time.time()
    episode = {"rows": [{"timestamp": now - 10, "content": "ordinary text"}] * 8}
    policy = conversation_policy.load_policy()
    assert gate._participation_gate({"chat_id": "synthetic", "content": "ordinary text"}, policy, episode)[0] is False
    assert gate._participation_gate({"chat_id": "synthetic", "content": "你觉得呢？"}, policy, episode)[0] is True
    from datetime import datetime
    hour = datetime.fromtimestamp(now).hour
    policy.update(proactive_quiet_start=hour, proactive_quiet_end=(hour + 1) % 24)
    assert gate._participation_gate({"chat_id": "synthetic", "content": "你觉得呢？"}, policy, episode)[1] == "quiet hours"


def test_new_settings_persist_and_validate(setup):
    saved = conversation_policy.save_policy({"proactive_daily_limit": 8,
        "proactive_allowed_moves": "追问、表态", "proactive_trigger_words": "电影、音乐"})
    assert conversation_policy.load_policy() == saved
    with pytest.raises(ValueError):
        conversation_policy.save_policy({"proactive_daily_limit": 1.5})
    with pytest.raises(ValueError):
        conversation_policy.save_policy({"proactive_trigger_words": ""})
