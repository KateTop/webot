"""Trigger and coordinate per-group memory consolidation."""

import concurrent.futures
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.store import MessageStore
    from ..summarize.base import AbstractSummarizer

logger = logging.getLogger(__name__)

MAX_NEW_MSGS_PER_CONSOLIDATION = 100
CONSOLIDATE_FAILURE_COOLDOWN_SEC = 600
CONSOLIDATE_CALL_TIMEOUT_SEC = 390


def _call_timeout(summarizer) -> int:
    """Bound the outer wait to the configured provider and retry budget."""
    providers = getattr(summarizer, "providers", [("primary", summarizer)])
    if not isinstance(providers, list):
        return CONSOLIDATE_CALL_TIMEOUT_SEC
    total = 0
    for _, provider in providers:
        attempts = max(1, int(getattr(provider, "max_retries", 3)))
        total += attempts * 25 + sum(min(2 ** n, 16) for n in range(1, attempts))
    return total + 60


class MemoryConsolidator:
    """Keep one consolidation request in flight per group."""

    def __init__(self, store: "MessageStore", summarizer: "AbstractSummarizer"):
        self._store = store
        self._summarizer = summarizer
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._timed_out: dict[str, concurrent.futures.Future] = {}
        self._retry_after: dict[str, float] = {}

    def check_and_consolidate(self, chat_id: str, force: bool = False,
                              through_row_id: int | None = None,
                              bypass_cooldown: bool = False) -> bool:
        """Consolidate when due; return False on skips or failures."""
        with self._lock:
            if chat_id in self._active:
                return False
            pending = self._timed_out.get(chat_id)
            if pending is not None:
                if not pending.done():
                    return False
                # Discard the late result. The next attempt reads the same
                # unprocessed messages from the store.
                del self._timed_out[chat_id]
            if not bypass_cooldown and time.monotonic() < self._retry_after.get(chat_id, 0):
                return False
            self._active.add(chat_id)

        try:
            return self._check_and_consolidate_impl(chat_id, force, through_row_id)
        except Exception:
            self._defer_retry(chat_id)
            logger.exception("Memory consolidation error for chat %s", chat_id[:30])
            return False
        finally:
            with self._lock:
                self._active.remove(chat_id)

    def save_manual_text(self, chat_id: str, text: str) -> None:
        """Save an edited soul only when no AI update can overwrite it."""
        with self._lock:
            if chat_id in self._active:
                raise RuntimeError("该群正在整理记忆，请稍后再保存")
            self._active.add(chat_id)
        try:
            self._store.save_group_memory_text(chat_id, text)
        finally:
            with self._lock:
                self._active.remove(chat_id)

    def consolidate_first_pending_segment(self, chat_id: str,
                                          end_row_id: int) -> bool:
        """Process only the earliest pending range so the cursor cannot skip data."""
        pending = self._store.list_pending_memory_segments(chat_id)
        segments = pending["segments"]
        if not segments or segments[0]["end_id"] != end_row_id:
            raise ValueError("请先整理最早的待处理时间段，或刷新列表")
        return self.check_and_consolidate(
            chat_id, force=True, through_row_id=end_row_id,
            bypass_cooldown=True,
        )

    def _defer_retry(self, chat_id: str) -> None:
        with self._lock:
            self._retry_after[chat_id] = time.monotonic() + CONSOLIDATE_FAILURE_COOLDOWN_SEC

    def _check_and_consolidate_impl(self, chat_id: str, force: bool = False,
                                    through_row_id: int | None = None) -> bool:
        from src.conversation_policy import load_policy

        memory = self._store.get_group_memory(chat_id)
        last_id = memory["last_message_id"] if memory else None
        if through_row_id is None and not force:
            policy = load_policy()
            pending = self._store.list_pending_memory_segments(chat_id, limit=200)
            selected = 0
            for segment in pending["segments"]:
                if not segment["closed"]:
                    break
                selected += segment["count"]
                through_row_id = segment["end_id"]
                if selected >= policy["memory_min_messages"]:
                    break
            if selected < policy["memory_min_messages"]:
                return False
        if through_row_id is None:
            new_count = self._store.get_new_message_count(chat_id, last_id)
        else:
            new_count = self._store.get_new_message_count(
                chat_id, last_id, through_row_id=through_row_id,
            )

        if new_count == 0:
            return False

        query_options = {"limit": MAX_NEW_MSGS_PER_CONSOLIDATION}
        if through_row_id is not None:
            query_options["through_row_id"] = through_row_id
        new_messages = self._store.get_messages_since_id(
            chat_id, last_id, **query_options,
        )
        if not new_messages:
            return False
        existing_memory = memory["memory_text"] if memory else ""
        logger.info(
            "Consolidating memory for %s (%d new msgs, existing memory=%d chars)...",
            chat_id[:30], len(new_messages), len(existing_memory),
        )

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(
            self._summarizer.consolidate_memory,
            existing_memory=existing_memory,
            new_messages=new_messages,
        )
        try:
            updated = future.result(timeout=_call_timeout(self._summarizer))
        except concurrent.futures.TimeoutError:
            with self._lock:
                self._timed_out[chat_id] = future
            self._defer_retry(chat_id)
            logger.warning("Memory consolidation timed out for %s", chat_id[:30])
            return False
        except Exception:
            self._defer_retry(chat_id)
            logger.exception("Memory consolidation request failed for %s", chat_id[:30])
            return False
        finally:
            # Python cannot stop a running request. Retain its future until
            # completion so another request for this group cannot stack.
            pool.shutdown(wait=False, cancel_futures=True)

        if not updated:
            self._defer_retry(chat_id)
            logger.info("Memory consolidation returned empty text for %s", chat_id[:30])
            return False

        total_count = (memory["message_count"] if memory else 0) + len(new_messages)
        self._store.upsert_group_memory(
            chat_id=chat_id,
            memory_text=updated,
            message_count=total_count,
            last_message_id=new_messages[-1]["message_id"],
        )
        with self._lock:
            self._retry_after.pop(chat_id, None)
        logger.info(
            "Memory consolidated for %s: %d msgs -> %d chars (total %d msgs processed)",
            chat_id[:30], len(new_messages), len(updated), total_count,
        )
        return True
