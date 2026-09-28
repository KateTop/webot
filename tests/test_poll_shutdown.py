"""Regression checks for the WCDB poll/executor shutdown race."""

from unittest.mock import MagicMock

from src.wechat.wcdb_backend import WcdbBackend
from src.web.server import _BotControl


def test_stop_leaves_executor_for_poll_thread_to_close():
    backend = WcdbBackend()
    backend._running = True
    backend._pool = MagicMock()
    backend.stop()
    assert backend._running is False
    assert backend._stop_requested is True
    backend._pool.shutdown.assert_not_called()


def _polling_backend():
    backend = WcdbBackend()
    backend._running = True
    backend._client = MagicMock()
    backend._client.get_messages.return_value = [{}]
    backend._pool = MagicMock()
    backend._standardize = MagicMock(return_value={
        "message_id": "synthetic-id", "sender_name": "Synthetic Sender",
    })
    return backend


def test_stop_during_message_processing_does_not_submit():
    backend = _polling_backend()
    backend._standardize.side_effect = lambda *_args: (backend.stop() or {
        "message_id": "synthetic-id", "sender_name": "Synthetic Sender",
    })
    backend._poll_group("Synthetic Group", "synthetic@chatroom", MagicMock())
    backend._pool.submit.assert_not_called()


def test_closed_executor_ends_poll_without_retry_loop():
    backend = _polling_backend()
    backend._pool.submit.side_effect = RuntimeError(
        "cannot schedule new futures after shutdown"
    )
    backend._poll_group("Synthetic Group", "synthetic@chatroom", MagicMock())
    assert backend._running is False
    assert backend._stop_requested is True


def test_stop_keeps_live_thread_registered_after_join_timeout():
    control = _BotControl()
    backend = MagicMock()
    thread = MagicMock()
    thread.is_alive.return_value = True
    control.register(thread=thread, backend=backend)
    assert control.stop() is False
    backend.stop.assert_called_once()
    thread.join.assert_called_once_with(timeout=30)
    assert control.is_running() is True
    assert control.thread is thread
    assert control.backend is backend


def test_start_slot_is_reserved_before_thread_runs():
    control = _BotControl()
    first = MagicMock()
    second = MagicMock()
    assert control.try_start(first) is True
    assert control.try_start(second) is False
    assert control.thread is first
    control.mark_stopped()
    assert control.try_start(second) is True
