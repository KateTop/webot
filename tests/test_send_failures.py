"""Persistent send failures: paging and duplicate-click protection."""

import tempfile
import unittest
from pathlib import Path

from src.db import MessageStore, initialize_db


class SendFailuresTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "messages.db")
        self.conn = initialize_db(self.path)
        self.store = MessageStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_paged_retry_and_restart(self):
        first = self.store.record_send_failure("group-a", "A", "first")
        second = self.store.record_send_failure("group-b", "B", "second")
        page = self.store.list_send_failures(1, 1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["id"], second)
        self.assertEqual(self.store.list_send_failures(2, 1)["items"][0]["id"], first)
        self.assertEqual(self.store.claim_send_failure(first)["content"], "first")
        self.assertIsNone(self.store.claim_send_failure(first))
        self.store.finish_send_failure(first, False)
        self.assertEqual(self.store.claim_send_failure(first)["chat_id"], "group-a")
        self.store.finish_send_failure(first, True)
        self.assertIsNone(self.store.claim_send_failure(first))
        self.assertEqual(self.store.list_send_failures(status="sent")["total"], 1)
        self.conn.close()
        self.conn = initialize_db(self.path)
        self.store = MessageStore(self.conn)
        self.assertEqual(self.store.list_send_failures(status="failed")["total"], 1)

    def test_interrupted_send_is_not_silently_retried(self):
        item_id = self.store.record_send_failure("group-a", "A", "content")
        self.store.claim_send_failure(item_id)
        self.assertEqual(self.store.mark_interrupted_sends(), 1)
        self.assertEqual(self.store.list_send_failures(status="uncertain")["total"], 1)


if __name__ == "__main__":
    unittest.main()
