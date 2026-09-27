"""Offline history import must be ordered, idempotent and send nothing."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.db import MessageStore, initialize_db
from src.memory.consolidator import MemoryConsolidator
from src.wechat.wcdb_backend import WcdbBackend


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self, talker, limit, offset):
        return self.messages[offset:offset + limit]


class HistoryBackfillTest(unittest.TestCase):
    def test_startup_memory_boundary_excludes_messages_arriving_during_drain(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = initialize_db(str(Path(folder) / "messages.db"))
            try:
                store = MessageStore(conn)
                for number in range(101):
                    store.insert_message({
                        "message_id": str(number), "chat_id": "a@chatroom",
                        "sender_id": "user", "sender_name": "User",
                        "content": "synthetic", "timestamp": number,
                    })
                boundary = store.get_latest_message_row_id("a@chatroom")
                batches = []

                def consolidate_memory(**kwargs):
                    batches.append([m["message_id"] for m in kwargs["new_messages"]])
                    if len(batches) == 1:
                        store.insert_message({
                            "message_id": "live", "chat_id": "a@chatroom",
                            "sender_id": "user", "sender_name": "User",
                            "content": "synthetic", "timestamp": 200,
                        })
                    return "memory"

                summarizer = Mock()
                summarizer.consolidate_memory.side_effect = consolidate_memory
                worker = MemoryConsolidator(store, summarizer)
                while worker.check_and_consolidate(
                        "a@chatroom", force=True, through_row_id=boundary):
                    pass
                self.assertEqual([len(batch) for batch in batches], [100, 1])
                self.assertEqual(batches[1], ["100"])
                self.assertEqual(store.get_new_message_count(
                    "a@chatroom", "100"), 1)
            finally:
                conn.close()

    def test_import_since_timestamp_without_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = initialize_db(str(Path(folder) / "messages.db"))
            try:
                store = MessageStore(conn)
                store.insert_message({"message_id": "100", "chat_id": "a@chatroom",
                                      "sender_id": "user", "sender_name": "User",
                                      "content": "prior", "timestamp": 100})
                backend = WcdbBackend(groups=["A"], store=store)
                backend._client = FakeClient([
                    {"id": str(ts), "create_time": ts} for ts in (104, 103, 102, 101, 100, 99)
                ])
                backend._standardize = lambda raw, group, talker, historical=False: {
                    "message_id": raw["id"], "chat_id": talker,
                    "sender_id": "user", "sender_name": "User",
                    "content": "synthetic", "timestamp": raw["create_time"],
                }
                self.assertEqual(backend._backfill_group("A", "a@chatroom"), 4)
                self.assertEqual(backend._backfill_group("A", "a@chatroom"), 0)
                self.assertEqual(store.get_latest_message_timestamp("a@chatroom"), 104)
                self.assertEqual(store.get_new_message_count("a@chatroom", None), 5)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
