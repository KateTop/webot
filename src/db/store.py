"""MessageStore — all database read/write operations.

Thread-safe: all public methods are serialized through a lock because
the underlying SQLite connection (check_same_thread=False) is not
safe for concurrent use from multiple threads.
"""

import sqlite3
import threading
import time
import logging
import hashlib
import os
import json
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MessageStore:
    """Wraps all database operations for message persistence and querying."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.Lock()
        self._trigger_count = 0
        database = conn.execute("PRAGMA database_list").fetchone()[2]
        self._memory_root = Path(database).resolve().parent / "memory" if database else None
        conn.execute("CREATE TABLE IF NOT EXISTS proactive_state (chat_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.commit()

    def get_proactive_state(self, chat_id: str) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT state FROM proactive_state WHERE chat_id=?", (chat_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_proactive_state(self, chat_id: str, state: dict) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO proactive_state VALUES (?, ?)",
                              (chat_id, json.dumps(state)))
            self.conn.commit()

    def _soul_path(self, chat_id: str) -> Path | None:
        if self._memory_root is None:
            return None
        folder = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:24]
        return self._memory_root / folder / "soul.md"

    def _read_soul(self, chat_id: str, fallback: str) -> str:
        path = self._soul_path(chat_id)
        if path is None:
            return fallback
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                logger.exception("Could not read soul.md for a group")
                return fallback
        if fallback:
            self._write_soul(chat_id, fallback)
        return fallback

    def _write_soul(self, chat_id: str, text: str) -> None:
        path = self._soul_path(chat_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def record_send_failure(self, chat_id: str, group_name: str, content: str) -> int:
        """Keep one record for a final failed send, after internal UI retries."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT INTO send_failures (chat_id, group_name, content) VALUES (?, ?, ?)",
                (chat_id, group_name, content),
            )
            return cursor.lastrowid

    def list_send_failures(self, page: int = 1, page_size: int = 20,
                           status: str = "failed") -> dict:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        if status not in ("failed", "sent", "retrying", "uncertain", "all"):
            raise ValueError("Invalid send failure status")
        where = "" if status == "all" else "WHERE status = ?"
        args = () if status == "all" else (status,)
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM send_failures {where}", args,
            ).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT id, chat_id, group_name, content, status, attempts, "
                f"created_at, updated_at FROM send_failures {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "page": page, "page_size": page_size}

    def claim_send_failure(self, failure_id: int) -> dict | None:
        """Atomically claim an item so two clicks cannot send it twice."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id, chat_id, group_name, content FROM send_failures "
                "WHERE id = ? AND status IN ('failed', 'uncertain')",
                (failure_id,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE send_failures SET status='retrying', attempts=attempts+1, "
                "updated_at=unixepoch() WHERE id=?", (failure_id,),
            )
            return dict(row)

    def finish_send_failure(self, failure_id: int, success: bool) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE send_failures SET status=?, updated_at=unixepoch() "
                "WHERE id=? AND status='retrying'",
                ("sent" if success else "failed", failure_id),
            )

    def mark_interrupted_sends(self) -> int:
        """A crash during send has unknown delivery; require human review."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE send_failures SET status='uncertain', updated_at=unixepoch() "
                "WHERE status='retrying'",
            )
            return cursor.rowcount

    # ── Write operations ──────────────────────────────────────────

    def insert_message(self, msg: dict) -> bool:
        """Insert a message and update the user's last-message cursor.

        Returns True if inserted, False if duplicate (silently skipped).
        """
        with self._lock:
            try:
                # Coerce all fields to SQLite-safe types (defensive).
                message_id = str(msg["message_id"])
                chat_id = str(msg["chat_id"])
                sender_id = str(msg["sender_id"])
                sender_name = str(msg["sender_name"])
                content = str(msg.get("content", ""))
                msg_type = int(msg.get("msg_type", 1))
                timestamp = int(msg.get("timestamp", 0))

                with self.conn:
                    self.conn.execute(
                        """INSERT INTO messages
                           (message_id, chat_id, sender_id, sender_name,
                            content, msg_type, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (message_id, chat_id, sender_id, sender_name,
                         content, msg_type, timestamp),
                    )
                    self.conn.execute(
                        """INSERT INTO user_last_message
                           (chat_id, sender_id, sender_name, last_timestamp)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(chat_id, sender_id) DO UPDATE SET
                           sender_name = CASE WHEN excluded.last_timestamp >= user_last_message.last_timestamp
                               THEN excluded.sender_name ELSE user_last_message.sender_name END,
                           last_timestamp = MAX(user_last_message.last_timestamp,
                                                excluded.last_timestamp)""",
                        (chat_id, sender_id, sender_name, timestamp),
                    )
                return True
            except sqlite3.IntegrityError:
                return False
            except sqlite3.InterfaceError:
                logger.warning(
                    "DB insert skipped (connection closed): msg_id=%s, chat=%s",
                    msg.get("message_id", "?")[:20], msg.get("chat_id", "?"),
                )
                return False
            except Exception:
                logger.exception(
                    "Failed to insert message (msg_id=%s, chat=%s, sender=%s)",
                    msg.get("message_id", "?"), msg.get("chat_id", "?"),
                    msg.get("sender_id", "?"),
                )
                return False

    def log_trigger(self, chat_id: str, requester_id: str,
                    trigger_msg_id: str) -> None:
        """Record a trigger event for deduplication.

        Periodically cleans old entries (every 100th trigger) and
        reclaims disk space (every 1000th trigger).
        """
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO trigger_log
                       (chat_id, requester_id, trigger_message_id)
                       VALUES (?, ?, ?)""",
                    (chat_id, requester_id, trigger_msg_id),
                )
            self._trigger_count += 1
            if self._trigger_count % 100 == 0:
                self._cleanup_old_triggers_locked()
            if self._trigger_count % 1000 == 0:
                self._vacuum_locked()

    def _cleanup_old_triggers_locked(self) -> int:
        """Delete trigger_log entries older than 7 days (caller must hold lock)."""
        cutoff = int(time.time()) - 7 * 86400
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM trigger_log WHERE processed_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
        if deleted:
            logger.info("Cleaned up %d old trigger_log entries.", deleted)
        return deleted

    def cleanup_old_triggers(self) -> int:
        """Delete trigger_log entries older than 7 days. Thread-safe."""
        with self._lock:
            return self._cleanup_old_triggers_locked()

    def _vacuum_locked(self) -> None:
        """Reclaim disk space from deleted trigger_log rows (caller must hold lock)."""
        logger.info("Running VACUUM to reclaim disk space.")
        with self.conn:
            # VACUUM rebuilds the database file, reclaiming freed pages.
            # PRAGMA optimize only runs ANALYZE — it doesn't shrink the file.
            self.conn.execute("VACUUM")

    # ── Query operations ───────────────────────────────────────────

    def get_latest_message_timestamp(self, chat_id: str) -> int | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(timestamp) FROM messages WHERE chat_id=?", (chat_id,),
            ).fetchone()
            return row[0] if row and row[0] is not None else None

    def get_sender_display_name(self, sender_id: str) -> Optional[str]:
        """Return a previously seen display name for a wxid, or None."""
        with self._lock:
            row = self.conn.execute(
                """SELECT sender_name FROM messages
                   WHERE sender_id = ? AND sender_name != sender_id
                   ORDER BY id DESC LIMIT 1""",
                (sender_id,),
            ).fetchone()
            return row["sender_name"] if row else None

    def get_user_last_timestamp(self, chat_id: str,
                                sender_id: str) -> Optional[int]:
        """Get the Unix timestamp of a user's most recent message in a chat."""
        with self._lock:
            row = self.conn.execute(
                """SELECT last_timestamp FROM user_last_message
                   WHERE chat_id = ? AND sender_id = ?""",
                (chat_id, sender_id),
            ).fetchone()
            return row["last_timestamp"] if row else None

    def get_user_previous_timestamp(self, chat_id: str,
                                    sender_id: str,
                                    before_ts: int) -> Optional[int]:
        """Get the timestamp of a user's last message BEFORE the given time."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT timestamp FROM messages
                   WHERE chat_id = ? AND sender_id = ? AND timestamp < ?
                   ORDER BY timestamp DESC
                   LIMIT 30""",
                (chat_id, sender_id, before_ts),
            ).fetchall()

            if not rows:
                return None

            prev_ts = before_ts
            skipped = 0
            for row in rows:
                gap = prev_ts - row["timestamp"]
                if gap > 30:
                    if skipped > 0:
                        logger.info(
                            "Skipped %d close prior messages from sender_id=%s "
                            "(final gap=%ds). Using earlier message as boundary.",
                            skipped, sender_id, gap,
                        )
                    return row["timestamp"]
                skipped += 1
                prev_ts = row["timestamp"]

            logger.info(
                "All %d prior messages from sender_id=%s are within close chain. "
                "Using oldest as boundary.",
                len(rows), sender_id,
            )
            return rows[-1]["timestamp"]

    def get_messages_since(self, chat_id: str, since_ts: int,
                           until_ts: Optional[int] = None,
                           limit: int = 500) -> list[dict]:
        """Fetch messages from a chat in a time window."""
        with self._lock:
            if until_ts is None:
                until_ts = int(time.time())

            rows = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, timestamp
                   FROM messages
                   WHERE chat_id = ? AND timestamp BETWEEN ? AND ?
                   ORDER BY timestamp ASC
                   LIMIT ?""",
                (chat_id, since_ts, until_ts, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_recent_messages(self, chat_id: str, before_ts: int,
                            limit: int = 30, exclude_id: str = "") -> list[dict]:
        """Latest messages in one group, returned in reading order."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, timestamp FROM messages
                   WHERE chat_id = ? AND
                     (timestamp < ? OR
                      (timestamp = ? AND rowid < COALESCE(
                          (SELECT rowid FROM messages WHERE message_id = ?),
                          9223372036854775807)))
                     AND message_id != ?
                   ORDER BY timestamp DESC, rowid DESC LIMIT ?""",
                (chat_id, before_ts, before_ts, exclude_id, exclude_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def search_group_messages(self, chat_id: str, keyword: str,
                              since_ts: int, limit: int = 20) -> list[dict]:
        """Search only one group; return recent evidence in chronological order."""
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name, content,
                          msg_type, timestamp FROM messages
                   WHERE chat_id = ? AND timestamp >= ?
                     AND content LIKE ? ESCAPE '\\'
                   ORDER BY timestamp DESC LIMIT ?""",
                (chat_id, since_ts, f"%{escaped}%", min(max(limit, 1), 20)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def was_recently_triggered(self, chat_id: str,
                                window_sec: int) -> bool:
        """Check if a trigger was processed for this chat recently."""
        with self._lock:
            cutoff = int(time.time()) - window_sec
            row = self.conn.execute(
                """SELECT COUNT(*) as cnt FROM trigger_log
                   WHERE chat_id = ? AND processed_at > ?""",
                (chat_id, cutoff),
            ).fetchone()
            return row["cnt"] > 0 if row else False

    # ── Group memory operations ────────────────────────────────────

    def get_group_memory(self, chat_id: str) -> dict | None:
        """Retrieve the memory record for a group."""
        try:
            with self._lock:
                row = self.conn.execute(
                    """SELECT chat_id, memory_text, message_count,
                              last_message_id, last_consolidated,
                              created_at, updated_at
                       FROM group_memory
                       WHERE chat_id = ?""",
                    (chat_id,),
                ).fetchone()
                if row is None:
                    return None
                memory = dict(row)
                memory["memory_text"] = self._read_soul(
                    chat_id, memory["memory_text"])
                return memory
        except sqlite3.InterfaceError:
            logger.debug("get_group_memory skipped: connection closed (shutting down)")
            return None

    def upsert_group_memory(self, chat_id: str, memory_text: str,
                            message_count: int, last_message_id: str) -> None:
        """Insert or update a group's memory record."""
        try:
            with self._lock:
                self._write_soul(chat_id, memory_text)
                now = time.time()
                with self.conn:
                    self.conn.execute(
                        """INSERT INTO group_memory
                           (chat_id, memory_text, message_count, last_message_id,
                            last_consolidated, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(chat_id) DO UPDATE SET
                           memory_text = excluded.memory_text,
                           message_count = excluded.message_count,
                           last_message_id = excluded.last_message_id,
                           last_consolidated = excluded.last_consolidated,
                           updated_at = excluded.updated_at""",
                        (chat_id, memory_text, message_count, last_message_id,
                         now, now, now),
                    )
        except sqlite3.InterfaceError:
            logger.debug("upsert_group_memory skipped: connection closed (shutting down)")

    def save_group_memory_text(self, chat_id: str, memory_text: str) -> None:
        """Edit one group's soul without advancing its consolidation cursor."""
        if not chat_id or len(memory_text) > 20000:
            raise ValueError("群 ID 不能为空，soul.md 不能超过 20000 字")
        with self._lock:
            self._write_soul(chat_id, memory_text)
            now = time.time()
            with self.conn:
                self.conn.execute(
                    """INSERT INTO group_memory
                       (chat_id, memory_text, message_count, last_message_id,
                        last_consolidated, created_at, updated_at)
                       VALUES (?, ?, 0, NULL, NULL, ?, ?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                       memory_text=excluded.memory_text,
                       updated_at=excluded.updated_at""",
                    (chat_id, memory_text, now, now),
                )

    def ensure_group_memory_file(self, chat_id: str) -> Path:
        """Create an editable soul.md without resetting an existing cursor."""
        if not chat_id:
            raise ValueError("群 ID 不能为空")
        with self._lock:
            row = self.conn.execute(
                "SELECT memory_text FROM group_memory WHERE chat_id=?", (chat_id,),
            ).fetchone()
            path = self._soul_path(chat_id)
            if path is None:
                raise RuntimeError("需要文件数据库才能创建 soul.md")
            if not path.exists():
                self._write_soul(chat_id, row[0] if row else "")
            if row is None:
                now = time.time()
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO group_memory (chat_id, created_at, updated_at) "
                        "VALUES (?, ?, ?)", (chat_id, now, now),
                    )
            return path

    def list_pending_memory_segments(self, chat_id: str,
                                     limit: int = 5000) -> dict:
        """Group unprocessed messages into settled conversation episodes."""
        from src.conversation_policy import load_policy
        from src.conversation_episodes import split_episodes
        if not chat_id:
            raise ValueError("群 ID 不能为空")
        limit = min(max(int(limit), 1), 5000)
        with self._lock:
            row = self.conn.execute(
                """SELECT m.id FROM group_memory g
                   LEFT JOIN messages m ON m.message_id=g.last_message_id
                   WHERE g.chat_id=?""", (chat_id,),
            ).fetchone()
            cursor_id = (row[0] or 0) if row else 0
            rows = self.conn.execute(
                """SELECT id, message_id, sender_id, content, timestamp FROM messages
                   WHERE chat_id=? AND id>? ORDER BY id ASC LIMIT ?""",
                (chat_id, cursor_id, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        episodes = split_episodes([dict(row) for row in rows[:limit]], load_policy())
        segments = [{key: value for key, value in part.items() if key != "rows"}
                    for part in episodes]
        return {"segments": segments, "has_more": has_more,
                "shown_count": len(rows), "cursor_id": cursor_id}

    def get_pending_segment_messages(self, chat_id: str, start_id: int,
                                     end_id: int) -> list[dict]:
        if not chat_id or start_id < 1 or end_id < start_id:
            raise ValueError("无效的消息范围")
        with self._lock:
            row = self.conn.execute(
                """SELECT m.id FROM group_memory g
                   LEFT JOIN messages m ON m.message_id=g.last_message_id
                   WHERE g.chat_id=?""", (chat_id,),
            ).fetchone()
            cursor_id = (row[0] or 0) if row else 0
            return [dict(item) for item in self.conn.execute(
                """SELECT id, sender_name, content, timestamp FROM messages
                   WHERE chat_id=? AND id BETWEEN ? AND ? AND id>?
                   ORDER BY id ASC LIMIT 100""",
                (chat_id, start_id, end_id, cursor_id),
            ).fetchall()]

    def get_latest_message_row_id(self, chat_id: str) -> int | None:
        """Capture the last stored row for a startup history pass."""
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(id) FROM messages WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            return row[0] if row else None

    def list_group_ids(self) -> list[str]:
        """List stored groups without loading their message bodies."""
        with self._lock:
            return [row[0] for row in self.conn.execute(
                "SELECT DISTINCT chat_id FROM messages ORDER BY chat_id",
            ).fetchall()]

    def get_new_message_count(self, chat_id: str,
                              since_message_id: str | None,
                              through_row_id: int | None = None) -> int:
        """Count new messages in a chat since a given message ID."""
        with self._lock:
            if since_message_id is None:
                row = self.conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE chat_id = ? "
                    "AND (? IS NULL OR id <= ?)",
                    (chat_id, through_row_id, through_row_id),
                ).fetchone()
            else:
                row = self.conn.execute(
                    """SELECT COUNT(*) as cnt FROM messages
                       WHERE chat_id = ? AND (? IS NULL OR id <= ?) AND id > (
                           SELECT COALESCE(
                               (SELECT id FROM messages WHERE message_id = ?), 0
                           )
                       )""",
                    (chat_id, through_row_id, through_row_id, since_message_id),
                ).fetchone()
            return row["cnt"] if row else 0

    def get_messages_since_id(self, chat_id: str,
                              since_message_id: str | None,
                              limit: int = 200,
                              through_row_id: int | None = None) -> list[dict]:
        """Fetch messages since a given message ID."""
        with self._lock:
            if since_message_id is None:
                rows = self.conn.execute(
                    """SELECT message_id, chat_id, sender_id, sender_name,
                              content, msg_type, timestamp
                       FROM messages
                       WHERE chat_id = ? AND (? IS NULL OR id <= ?)
                       ORDER BY id ASC
                       LIMIT ?""",
                    (chat_id, through_row_id, through_row_id, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT message_id, chat_id, sender_id, sender_name,
                              content, msg_type, timestamp
                       FROM messages
                       WHERE chat_id = ? AND (? IS NULL OR id <= ?) AND id > (
                           SELECT COALESCE(
                               (SELECT id FROM messages WHERE message_id = ?), 0
                           )
                       )
                       ORDER BY id ASC
                       LIMIT ?""",
                    (chat_id, through_row_id, through_row_id, since_message_id, limit),
                ).fetchall()
            return [dict(row) for row in rows]
