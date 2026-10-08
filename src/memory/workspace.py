"""Versioned memory commits, snapshots, redaction and periodic maintenance."""
import json
import threading
import time
from contextlib import contextmanager
from .document import parse, render, digest, migration_preview, apply_operations

_locks, _guard = {}, threading.Lock()


class MemoryConflict(RuntimeError):
    pass


class MemoryWorkspace:
    def __init__(self, store):
        self.store = store
        with store._lock:
            store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_versions(chat_id TEXT PRIMARY KEY, version INTEGER DEFAULT 0, maintenance_day TEXT, maintenance_hash TEXT);
            CREATE TABLE IF NOT EXISTS memory_snapshots(id INTEGER PRIMARY KEY, chat_id TEXT, version INTEGER, text TEXT, created_at REAL);
            CREATE TABLE IF NOT EXISTS memory_blocks(chat_id TEXT, subject_id TEXT, topic TEXT, PRIMARY KEY(chat_id,subject_id,topic));
            CREATE TABLE IF NOT EXISTS memory_audit(id INTEGER PRIMARY KEY, chat_id TEXT, operations TEXT, created_at REAL);
            CREATE TABLE IF NOT EXISTS assistant_outbox(id INTEGER PRIMARY KEY, chat_id TEXT, content TEXT, action TEXT, status TEXT, message_id TEXT, created_at REAL);
            CREATE TABLE IF NOT EXISTS authorized_reminders(id INTEGER PRIMARY KEY, chat_id TEXT, subject_id TEXT, text TEXT, due REAL, status TEXT, confirmation_message_id TEXT);
            """)
            columns = {r[1] for r in store.conn.execute("PRAGMA table_info(memory_versions)")}
            if "maintenance_hash" not in columns:
                store.conn.execute("ALTER TABLE memory_versions ADD COLUMN maintenance_hash TEXT")
            store.conn.commit()

    @contextmanager
    def group_lock(self, chat_id):
        with self.store._lock:
            db = self.store.conn.execute("PRAGMA database_list").fetchone()[2]
        with _guard:
            lock = _locks.setdefault((db or id(self.store.conn), chat_id), threading.RLock())
        with lock:
            yield

    def read(self, chat_id):
        memory = self.store.get_group_memory(chat_id) or {}
        text = memory.get("memory_text", "")
        with self.store._lock:
            row = self.store.conn.execute("SELECT version FROM memory_versions WHERE chat_id=?", (chat_id,)).fetchone()
        return text, (row[0] if row else 0, digest(text))

    def blocks(self, chat_id):
        with self.store._lock:
            return [dict(r) for r in self.store.conn.execute("SELECT subject_id,topic FROM memory_blocks WHERE chat_id=?", (chat_id,))]

    def filtered_text(self, chat_id):
        text, _ = self.read(chat_id)
        blocked = self.blocks(chat_id)
        if not blocked:
            return text
        try:
            entries = parse(text)
            return render([e for e in entries if not any(e.get("subject_id") == b["subject_id"] and
                (b["topic"] in e.get("topic", "") or b["topic"] in e["text"]) for b in blocked)])
        except ValueError:
            return ""  # Fail closed while a legacy document is being migrated.

    def commit(self, chat_id, text, token, last_message_id=None, processed=0, audit=None):
        parse(text)
        with self.group_lock(chat_id), self.store._lock:
            conn = self.store.conn
            conn.execute("BEGIN IMMEDIATE")
            old, projection_written = None, False
            try:
                row = conn.execute("SELECT memory_text FROM group_memory WHERE chat_id=?", (chat_id,)).fetchone()
                old = self.store._read_soul(chat_id, row[0] if row else "")
                v = conn.execute("SELECT version FROM memory_versions WHERE chat_id=?", (chat_id,)).fetchone()
                version = v[0] if v else 0
                if token != (version, digest(old)):
                    raise MemoryConflict("记忆已被编辑，请重新读取后重试")
                now = time.time()
                if old != text:
                    conn.execute("INSERT INTO memory_snapshots(chat_id,version,text,created_at) VALUES(?,?,?,?)", (chat_id,version,old,now))
                conn.execute("INSERT INTO group_memory(chat_id,memory_text,message_count,last_message_id,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET memory_text=excluded.memory_text,message_count=message_count+?,last_message_id=COALESCE(excluded.last_message_id,last_message_id),updated_at=excluded.updated_at",
                    (chat_id,text,processed,last_message_id,now,processed))
                conn.execute("INSERT INTO memory_versions(chat_id,version) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET version=excluded.version", (chat_id,version+1))
                conn.execute("DELETE FROM memory_snapshots WHERE chat_id=? AND id NOT IN (SELECT id FROM memory_snapshots WHERE chat_id=? ORDER BY id DESC LIMIT 5)", (chat_id,chat_id))
                if audit:
                    conn.execute("INSERT INTO memory_audit(chat_id,operations,created_at) VALUES(?,?,?)", (chat_id,json.dumps(audit),now))
                if processed:
                    conn.execute("UPDATE group_memory SET last_consolidated=? WHERE chat_id=?",(now,chat_id))
                self.store._write_soul(chat_id, text)
                projection_written = True
                conn.commit()
            except Exception:
                conn.rollback()
                if projection_written and old is not None:
                    self.store._write_soul(chat_id, old)
                raise

    def migrate(self, chat_id, preview, token):
        self.commit(chat_id, preview, token, audit=[{"action":"migration"}])

    def snapshots(self, chat_id):
        with self.store._lock:
            return [dict(r) for r in self.store.conn.execute("SELECT id,version,created_at FROM memory_snapshots WHERE chat_id=? ORDER BY id DESC", (chat_id,))]

    def restore(self, chat_id, snapshot_id):
        with self.group_lock(chat_id):
            with self.store._lock:
                row = self.store.conn.execute("SELECT text FROM memory_snapshots WHERE chat_id=? AND id=?", (chat_id,snapshot_id)).fetchone()
            if row is None:
                raise ValueError("快照不存在")
            restored = migration_preview(row[0])
            _, token = self.read(chat_id)
            self.commit(chat_id,restored,token,audit=[{"action":"restore","snapshot_id":snapshot_id}])

    def forget(self, chat_id, subject, ids, topic):
        if not subject or not topic or len(topic) > 100:
            raise ValueError("遗忘需要本人 ID 和粗粒度主题")
        with self.group_lock(chat_id):
            text, token = self.read(chat_id)
            entries = parse(text)
            by_id = {e["id"]: e for e in entries}
            if not ids or any(i not in by_id or by_id[i].get("subject_id") != subject for i in ids):
                raise ValueError("无权删除其他人或身份不明的记忆")
            # Install the read/write block first. Interrupted deletion remains suppressed.
            with self.store._lock, self.store.conn:
                self.store.conn.execute("INSERT OR IGNORE INTO memory_blocks VALUES(?,?,?)", (chat_id,subject,topic))
            def scrub(source):
                try:
                    return render([e for e in parse(source) if e["id"] not in ids and not
                        (e.get("subject_id") == subject and (topic in e.get("topic", "") or topic in e["text"]))])
                except ValueError:
                    return render([])  # Legacy snapshots cannot be safely redacted by owner.
            self.commit(chat_id,scrub(text),token,audit=[{"action":"forget","ids":ids}])
            with self.store._lock, self.store.conn:
                rows = self.store.conn.execute("SELECT id,text FROM memory_snapshots WHERE chat_id=?", (chat_id,)).fetchall()
                for row in rows:
                    self.store.conn.execute("UPDATE memory_snapshots SET text=? WHERE id=?", (scrub(row[1]),row[0]))
                self.store.conn.execute("UPDATE authorized_reminders SET status='cancelled' WHERE chat_id=? AND subject_id=? AND text LIKE ?", (chat_id,subject,"%"+topic+"%"))

    def preview(self, chat_id):
        text, token = self.read(chat_id)
        return {"text":migration_preview(text), "token":list(token)}
