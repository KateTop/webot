"""Regression tests for failed and overlapping memory consolidation."""

import threading
import time
from unittest.mock import Mock, patch

from src.memory import consolidator as module
from src.summarize.base import AbstractSummarizer


def _fixture(count=50, result="updated"):
    messages = [{"message_id": str(i), "content": "hello"} for i in range(count)]
    store = Mock()
    store.get_group_memory.return_value = None
    store.get_new_message_count.return_value = count
    store.get_messages_since_id.return_value = messages
    summarizer = Mock()
    summarizer.consolidate_memory.return_value = result
    return module.MemoryConsolidator(store, summarizer), store, summarizer


def test_first_consolidation_waits_for_message_threshold():
    consolidator, store, summarizer = _fixture(count=1)
    assert consolidator.check_and_consolidate("group") is False
    summarizer.consolidate_memory.assert_not_called()
    store.get_messages_since_id.assert_not_called()


def test_failure_cools_down_only_its_group():
    consolidator, store, summarizer = _fixture(result="")
    assert consolidator.check_and_consolidate("group-a") is False
    assert consolidator.check_and_consolidate("group-a") is False
    assert consolidator.check_and_consolidate("group-b") is False
    assert summarizer.consolidate_memory.call_count == 2
    store.upsert_group_memory.assert_not_called()


def test_same_group_cannot_overlap():
    entered = threading.Event()
    release = threading.Event()
    consolidator, store, summarizer = _fixture()

    def blocked(**_kwargs):
        entered.set()
        release.wait(5)
        return "updated"

    summarizer.consolidate_memory.side_effect = blocked
    first = threading.Thread(target=consolidator.check_and_consolidate, args=("group",))
    first.start()
    try:
        assert entered.wait(2)
        assert consolidator.check_and_consolidate("group") is False
        assert summarizer.consolidate_memory.call_count == 1
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
    store.upsert_group_memory.assert_called_once()


def test_timeout_waits_for_original_request_even_after_cooldown():
    release = threading.Event()
    consolidator, store, summarizer = _fixture()

    def blocked(**_kwargs):
        release.wait(5)
        return "late result"

    summarizer.consolidate_memory.side_effect = blocked
    with patch.object(module, "CONSOLIDATE_CALL_TIMEOUT_SEC", 0.01), \
         patch.object(module, "CONSOLIDATE_FAILURE_COOLDOWN_SEC", 0):
        try:
            assert consolidator.check_and_consolidate("group") is False
            assert consolidator.check_and_consolidate("group") is False
            assert summarizer.consolidate_memory.call_count == 1
            store.upsert_group_memory.assert_not_called()
        finally:
            release.set()
    # Wait only for the test worker; its late result must not write memory.
    for _ in range(100):
        if consolidator._timed_out["group"].done():
            break
        time.sleep(0.01)
    store.upsert_group_memory.assert_not_called()


def test_last_retry_has_no_sleep():
    summarizer = Mock(spec=AbstractSummarizer)
    summarizer.max_retries = 3
    summarizer.retry_exceptions = (ConnectionError,)
    call = Mock(side_effect=ConnectionError("offline"))
    with patch("src.summarize.base.time.sleep") as sleep:
        try:
            AbstractSummarizer._retry_with_backoff(summarizer, call, "memory consolidation")
        except RuntimeError:
            pass
        else:
            assert False, "expected exhausted retry error"
    assert call.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [2, 4]
