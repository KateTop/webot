"""Timeout-only AI provider rotation."""

import unittest

from unittest.mock import patch

from src.config import BotConfig
from src.summarize import create_summarizer
from src.summarize.base import AbstractSummarizer
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


class RetryingTimeoutProvider(FakeProvider):
    max_retries = 4
    retry_exceptions = (TimeoutError,)

    def chat(self, *args, **kwargs):
        def request():
            self.calls += 1
            raise TimeoutError("synthetic timeout")
        return AbstractSummarizer._retry_with_backoff(self, request, "chat")


class FailoverTest(unittest.TestCase):
    def test_timeout_uses_next_provider(self):
        primary = RetryingTimeoutProvider()
        backup = FakeProvider(result="ok")
        pool = FailoverSummarizer([("first", primary), ("second", backup)])
        with patch("src.summarize.base.time.sleep") as sleep:
            self.assertEqual(pool.chat("question"), "ok")
        self.assertEqual((primary.calls, backup.calls), (4, 1))
        self.assertEqual(sleep.call_count, 3)

    def test_factory_uses_four_attempts_per_pooled_provider(self):
        config = BotConfig(ai_backend="deepseek", fallback_backends=["openai"],
                           deepseek_api_key="test-key", openai_api_key="test-key")
        pool = create_summarizer(config)
        self.assertEqual([p.max_retries for _, p in pool.providers], [4, 4])

    def test_configuration_error_does_not_rotate(self):
        primary = FakeProvider(error=ValueError("bad key"))
        backup = FakeProvider(result="ok")
        pool = FailoverSummarizer([("first", primary), ("second", backup)])
        with self.assertRaises(ValueError):
            pool.chat("question")
        self.assertEqual(backup.calls, 0)


if __name__ == "__main__":
    unittest.main()
