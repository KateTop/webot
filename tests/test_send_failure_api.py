"""Local failure-list API and manual retry flow without sending to WeChat."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import MessageStore, initialize_db
from tests.test_web_api import _build_handler


def response(sock):
    return json.loads(sock.get_response_text().split("\r\n\r\n", 1)[1])


class FakeBackend:
    def __init__(self):
        self.calls = []

    def retry_failed_send(self, chat_id, content):
        self.calls.append((chat_id, content))
        return True


class SendFailureApiTest(unittest.TestCase):
    def test_list_then_retry_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "messages.db")
            conn = initialize_db(path)
            item_id = MessageStore(conn).record_send_failure("g@chatroom", "Group", "text")
            conn.close()
            backend = FakeBackend()
            with patch("src.web.server._message_db_path", return_value=path), \
                 patch("src.web.server._bot_control") as control:
                control.is_running.return_value = True
                control.backend = backend
                _, sock = _build_handler("/api/send-failures?page=1&page_size=1")
                listing = response(sock)
                self.assertEqual(listing["total"], 1)
                self.assertEqual(listing["items"][0]["id"], item_id)
                body = json.dumps({"id": item_id}).encode()
                _, sock = _build_handler("/api/send-failures/retry", "POST", body,
                                         {"Content-Type": "application/json"})
                self.assertTrue(response(sock)["ok"], response(sock))
                self.assertEqual(backend.calls, [("g@chatroom", "text")])
                _, sock = _build_handler("/api/send-failures/retry", "POST", body,
                                         {"Content-Type": "application/json"})
                self.assertFalse(response(sock)["ok"])
                self.assertEqual(len(backend.calls), 1)


if __name__ == "__main__":
    unittest.main()
