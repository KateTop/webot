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
        from .workspace import MemoryWorkspace
        self.workspace = MemoryWorkspace(store)

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
            with self.workspace.group_lock(chat_id):
                from .workspace import MemoryConflict
                for attempt in range(2):
                    try:
                        return self._check_and_consolidate_impl(chat_id, force, through_row_id)
                    except MemoryConflict:
                        if attempt:
                            raise
                return False
        except Exception:
            self._defer_retry(chat_id)
            logger.exception("Memory consolidation error for chat %s", chat_id[:30])
            return False
        finally:
            with self._lock:
                self._active.remove(chat_id)

    def save_manual_text(self, chat_id: str, text: str, expected_token=None) -> None:
        """Save an edited soul only when no AI update can overwrite it."""
        with self._lock:
            if chat_id in self._active:
                raise RuntimeError("该群正在整理记忆，请稍后再保存")
            self._active.add(chat_id)
        try:
            from .document import parse
            parse(text)
            _, token = self.workspace.read(chat_id)
            self.workspace.commit(chat_id, text, tuple(expected_token) if expected_token is not None else token)
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
                # Slow, continuously active group: keep the latest ten minutes.
                rows = self._store.get_messages_since_id(chat_id, last_id, limit=100)
                eligible = [r for r in rows if r["timestamp"] <= time.time() - policy["memory_tail_sec"]]
                effective = [r for r in eligible if self._effective(r)]
                if (not rows or time.time() - rows[0]["timestamp"] < policy["memory_fallback_sec"]
                        or len(effective) < policy["memory_min_messages"]):
                    return False
                with self._store._lock:
                    row = self._store.conn.execute("SELECT id FROM messages WHERE message_id=?", (eligible[-1]["message_id"],)).fetchone()
                through_row_id = row[0]
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
        from .document import parse, render, apply_write, NO_UPDATE
        existing_memory, token = self.workspace.read(chat_id)
        old_entries = parse(existing_memory)
        if not force and len(new_messages) < 100 and sum(self._effective(m) for m in new_messages) < load_policy()["memory_min_messages"]:
            return False
        with self._store._lock:
            prior = self._store.conn.execute("SELECT message_id,sender_id,sender_name,content,timestamp FROM messages WHERE chat_id=? AND id < (SELECT id FROM messages WHERE message_id=?) ORDER BY id DESC LIMIT 5", (chat_id,new_messages[0]["message_id"])).fetchall()
            own = self._store.conn.execute("SELECT * FROM assistant_outbox WHERE chat_id=? AND status='confirmed' AND action IN ('表态','反对') ORDER BY id DESC LIMIT 10", (chat_id,)).fetchall()
        model_rows = [dict(m) for m in new_messages]
        action_by_id = {r["message_id"]:r["action"] for r in own}
        for message in model_rows:
            if message.get("sender_id") == "__assistant__":
                message["action"] = action_by_id.get(message["message_id"], "")
        model_rows += [dict(message_id='assistant:'+str(r['id']), sender_id='__assistant__', sender_name='我', content=r['content'], action=r['action'], timestamp=r['created_at']) for r in own]
        context_only = [dict(r) for r in reversed(prior)]
        logger.info(
            "Consolidating memory for %s (%d new msgs, existing memory=%d chars)...",
            chat_id[:30], len(new_messages), len(existing_memory),
        )

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        def request_and_validate():
            for attempt in range(2):
                result = self._summarizer.memory_request("write", old_entries, model_rows,
                    self.workspace.blocks(chat_id), request={"context_only": context_only, "instruction":"上文已经处理，只用于理解，不能作为新增证据"})
                try:
                    if not result or not result.strip():
                        raise ValueError("空结果不是 NO_UPDATE")
                    return result, apply_write(old_entries, result, model_rows, self.workspace.blocks(chat_id))
                except (ValueError, TypeError, KeyError):
                    if attempt:
                        raise
            raise ValueError("记忆验收失败")
        future = pool.submit(request_and_validate)
        try:
            result, entries = future.result(timeout=_call_timeout(self._summarizer) * 2)
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

        updated = existing_memory if result.strip() == NO_UPDATE and existing_memory else render(entries)
        self.workspace.commit(chat_id, updated, token,
            last_message_id=new_messages[-1]["message_id"], processed=len(new_messages))
        total_count = (memory["message_count"] if memory else 0) + len(new_messages)
        with self._lock:
            self._retry_after.pop(chat_id, None)
        logger.info(
            "Memory consolidated for %s: %d msgs -> %d chars (total %d msgs processed)",
            chat_id[:30], len(new_messages), len(updated), total_count,
        )
        return True

    @staticmethod
    def _effective(message):
        import re
        text = str(message.get("content", "")).strip()
        return len(text) > 3 and not re.fullmatch(r"(?:\[[^\]]+\]\s*)+", text) and any(c.isalnum() for c in text)

    def maintenance(self, chat_id, now=None, force=False):
        import datetime
        from .document import parse, render, apply_operations
        now = time.time() if now is None else now
        today = datetime.datetime.fromtimestamp(now).date().isoformat()
        if not force and datetime.datetime.fromtimestamp(now).hour >= 6:
            return False
        with self.workspace.group_lock(chat_id):
            text, token = self.workspace.read(chat_id)
            entries = parse(text)
            if not entries:
                return False
            with self._store._lock:
                previous = self._store.conn.execute("SELECT maintenance_day,maintenance_hash FROM memory_versions WHERE chat_id=?", (chat_id,)).fetchone()
                latest = self._store.conn.execute("SELECT MAX(timestamp) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
            if not force and (previous and previous[0] == today or latest and now-latest < 1800):
                return False
            kept = [e for e in entries if not (e["section"] == "观察中" and e.get("dates") and
                (datetime.date.fromisoformat(today)-datetime.date.fromisoformat(max(e["dates"]))).days > 30)]
            if not force and len(kept) == len(entries) and previous and previous[1] == token[1]:
                return False
            # Empty/no-change memory can be settled cheaply without an API request.
            proposal = self._summarizer.memory_request("settle", kept) if kept else "NO_UPDATE"
            updated, audit = apply_operations(kept, proposal)
            changed = render(updated) != text
            if changed:
                self.workspace.commit(chat_id,render(updated),token,audit=audit or [{"action":"maintenance"}])
            with self._store._lock, self._store.conn:
                self._store.conn.execute("UPDATE memory_versions SET maintenance_day=?,maintenance_hash=? WHERE chat_id=?", (today,__import__("hashlib").sha256(render(updated).encode()).hexdigest(),chat_id))
            return True

    def remember_request(self, msg):
        from .document import parse,render,apply_write
        from .workspace import MemoryConflict
        with self.workspace.group_lock(msg["chat_id"]):
            for attempt in range(2):
                text,token=self.workspace.read(msg["chat_id"])
                entries=parse(text)
                result=self._summarizer.memory_request("write",entries,[msg],self.workspace.blocks(msg["chat_id"]),
                    request={"instruction":"本人明确要求记住；仅处理本人请求，不擅自加入第三方信息"})
                updated=apply_write(entries,result,[msg],self.workspace.blocks(msg["chat_id"]))
                changed=[e for e in updated if e not in entries]
                if any(e.get("subject_id") != msg["sender_id"] for e in changed):
                    raise ValueError("明确记住请求只能更新本人所属条目")
                if result == "NO_UPDATE":
                    return "这条信息没有形成新的记忆变更；你可以在记忆栏检查已有记录。"
                try:
                    self.workspace.commit(msg["chat_id"],render(updated),token)
                    return "记下了。你可以在记忆栏查看或修改这条记录。"
                except MemoryConflict:
                    if attempt:
                        raise

    def forget_request(self, msg):
        from .document import parse
        chat_id, subject = msg["chat_id"], msg["sender_id"]
        with self.workspace.group_lock(chat_id):
            text, _ = self.workspace.read(chat_id)
            entries = [e for e in parse(text) if e.get("subject_id") == subject]
            response = self._summarizer.memory_request("forget", entries, request={"subject_id":subject,"message":msg["content"]})
            if response == "NO_UPDATE":
                return "我没找到能够明确对应的本人记忆，请说明要忘记的主题。"
            import json
            data = json.loads(response)
            from .document import apply_operations
            apply_operations(entries,response,forget_subject=subject)
            ids = [i for op in data["operations"] for i in op["ids"]]
            by_id = {e["id"]:e for e in entries}
            topics = {by_id[i].get("topic", "") for i in ids}
            for topic in topics:
                self.workspace.forget(chat_id,subject,[i for i in ids if by_id[i].get("topic") == topic],topic)
            return "我不会再提或记录这些主题了；原始聊天记录仍保留在聊天库中。"
