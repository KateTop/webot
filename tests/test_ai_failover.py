"""Timeout-only AI provider rotation."""

import unittest

from src.summarize.base import BackendTimeoutError
from src.summarize.failover import FailoverSummarizer


class FakeProvider:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.last_api_call_time = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FailoverTest(unittest.TestCase):
    def test_timeout_uses_next_provider(self):
        primary = FakeProvider(error=BackendTimeoutError("timed out"))
        backup = FakeProvider(result="ok")
        pool = FailoverSummarizer([("first", primary), ("second", backup)])
        self.assertEqual(pool.chat("question"), "ok")
        self.assertEqual((primary.calls, backup.calls), (1, 1))

    def test_configuration_error_does_not_rotate(self):
        primary = FakeProvider(error=ValueError("bad key"))
        backup = FakeProvider(result="ok")
        pool = FailoverSummarizer([("first", primary), ("second", backup)])
        with self.assertRaises(ValueError):
            pool.chat("question")
        self.assertEqual(backup.calls, 0)


if __name__ == "__main__":
    unittest.main()
