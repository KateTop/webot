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

CONSOLIDATE_MSG_THRESHOLD = 50
CONSOLIDATE_TIME_THRESHOLD_SEC = 3600
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
                              through_row_id: int | None = None) -> bool:
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
            if time.monotonic() < self._retry_after.get(chat_id, 0):
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

    def _defer_retry(self, chat_id: str) -> None:
        with self._lock:
            self._retry_after[chat_id] = time.monotonic() + CONSOLIDATE_FAILURE_COOLDOWN_SEC

    def _check_and_consolidate_impl(self, chat_id: str, force: bool = False,
                                    through_row_id: int | None = None) -> bool:
        memory = self._store.get_group_memory(chat_id)
        last_id = memory["last_message_id"] if memory else None
        last_consolidated = memory["last_consolidated"] if memory else None
        if through_row_id is None:
            new_count = self._store.get_new_message_count(chat_id, last_id)
        else:
            new_count = self._store.get_new_message_count(
                chat_id, last_id, through_row_id=through_row_id,
            )

        # An uninitialized group has no time origin. Wait for a batch.
        time_due = (last_consolidated is not None and
                    time.time() - last_consolidated >= CONSOLIDATE_TIME_THRESHOLD_SEC)
        if new_count == 0 or not (force or new_count >= CONSOLIDATE_MSG_THRESHOLD or time_due):
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
