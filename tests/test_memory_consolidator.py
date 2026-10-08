"""Memory protocol regression tests against synthetic SQLite stores."""
import threading
import time
from unittest.mock import Mock, patch
from src.db import MessageStore, initialize_db
from src.memory import consolidator as module
from src.summarize.base import AbstractSummarizer


def _fixture(count=50, result="NO_UPDATE"):
    store = MessageStore(initialize_db(":memory:"))
    for group in ("group", "group-a", "group-b"):
        for i in range(count):
            store.insert_message(dict(message_id=f"{group}:{i}",chat_id=group,sender_id="u",sender_name="User",content="synthetic conversation",timestamp=100+i))
    summarizer=Mock()
    summarizer.memory_request.return_value=result
    return module.MemoryConsolidator(store,summarizer),store,summarizer


def test_first_consolidation_waits_for_message_threshold():
    worker,store,ai=_fixture(1)
    assert not worker.check_and_consolidate("group")
    ai.memory_request.assert_not_called()


def test_history_import_can_force_small_batch():
    worker,store,ai=_fixture(1)
    assert worker.check_and_consolidate("group",force=True)
    assert store.get_group_memory("group")["last_message_id"] == "group:0"


def test_small_closed_episode_does_not_block_following_full_episode():
    worker,store,ai=_fixture(100)
    store.list_pending_memory_segments=Mock(return_value={"segments":[{"closed":True,"count":2,"end_id":2},{"closed":True,"count":100,"end_id":102}]})
    assert worker.check_and_consolidate("group")
    assert store.get_group_memory("group")["message_count"] == 100


def test_no_update_advances_without_rewriting_memory():
    worker,store,ai=_fixture(1)
    assert worker.check_and_consolidate("group",force=True)
    before=store.get_group_memory("group")["memory_text"]
    store.insert_message(dict(message_id="next",chat_id="group",sender_id="u",sender_name="User",content="more synthetic text",timestamp=150))
    assert worker.check_and_consolidate("group",force=True)
    assert store.get_group_memory("group")["memory_text"] == before
    assert store.get_group_memory("group")["last_message_id"] == "next"


def test_failure_cools_down_only_its_group():
    worker,store,ai=_fixture(result="")
    assert not worker.check_and_consolidate("group-a")
    assert not worker.check_and_consolidate("group-a")
    assert not worker.check_and_consolidate("group-b")
    assert ai.memory_request.call_count == 4
    assert store.get_group_memory("group-a") is None


def test_same_group_cannot_overlap():
    entered,release=threading.Event(),threading.Event()
    worker,store,ai=_fixture()
    def blocked(*args,**kwargs):
        entered.set();release.wait(5);return "NO_UPDATE"
    ai.memory_request.side_effect=blocked
    thread=threading.Thread(target=worker.check_and_consolidate,args=("group",));thread.start()
    try:
        assert entered.wait(2)
        assert not worker.check_and_consolidate("group")
        assert ai.memory_request.call_count == 1
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive()
    assert store.get_group_memory("group")["last_message_id"] == "group:49"


def test_timeout_does_not_write_late_result():
    release=threading.Event()
    worker,store,ai=_fixture()
    ai.memory_request.side_effect=lambda *a,**k: release.wait(5) and "NO_UPDATE"
    with patch.object(module,"_call_timeout",return_value=.01),patch.object(module,"CONSOLIDATE_FAILURE_COOLDOWN_SEC",0):
        try:
            assert not worker.check_and_consolidate("group")
            assert not worker.check_and_consolidate("group")
            assert ai.memory_request.call_count == 1
        finally:
            release.set()
    worker._timed_out["group"].result(timeout=1)
    assert store.get_group_memory("group") is None


def test_last_retry_has_no_sleep():
    ai=Mock(spec=AbstractSummarizer);ai.max_retries=3;ai.retry_exceptions=(ConnectionError,)
    call=Mock(side_effect=ConnectionError("offline"))
    with patch("src.summarize.base.time.sleep") as sleep:
        try: AbstractSummarizer._retry_with_backoff(ai,call,"memory")
        except RuntimeError: pass
    assert call.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [2,4]
