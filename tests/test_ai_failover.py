"""AI provider rotation after exhausted retryable failures."""

import unittest

from unittest.mock import patch
import httpx2
from openai import AuthenticationError, InternalServerError

from src.config import BotConfig
from src.summarize import create_summarizer
from src.summarize.base import AbstractSummarizer, BackendTransientError
from src.summarize.failover import FailoverSummarizer
from src.summarize.openai_backend import OpenAISummarizer


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


class ServiceUnavailable(Exception):
    status_code = 503


class AuthenticationFailed(Exception):
    status_code = 401


class RetryingServiceProvider(FakeProvider):
    max_retries = 4
    retry_exceptions = (ServiceUnavailable,)

    def consolidate_memory(self, *args, **kwargs):
        def request():
            self.calls += 1
            raise ServiceUnavailable("synthetic 503")
        return AbstractSummarizer._retry_with_backoff(self, request, "memory consolidation")


class FailoverTest(unittest.TestCase):
    def test_openai_sdk_503_becomes_rotatable_error(self):
        provider = OpenAISummarizer.__new__(OpenAISummarizer)
        provider.max_retries = 2
        response = httpx2.Response(
            503, request=httpx2.Request('POST', 'https://example.test/v1/chat/completions'))

        def fail():
            raise InternalServerError('synthetic unavailable', response=response, body={})

        with patch('src.summarize.base.time.sleep'):
            with self.assertRaises(BackendTransientError):
                provider._retry_with_backoff(fail, 'memory consolidation')

    def test_openai_sdk_401_is_not_retried(self):
        provider = OpenAISummarizer.__new__(OpenAISummarizer)
        provider.max_retries = 4
        response = httpx2.Response(
            401, request=httpx2.Request('POST', 'https://example.test/v1/chat/completions'))
        calls = []

        def fail():
            calls.append(1)
            raise AuthenticationError('synthetic bad key', response=response, body={})

        with self.assertRaises(AuthenticationError):
            provider._retry_with_backoff(fail, 'chat')
        self.assertEqual(len(calls), 1)

    def test_exhausted_503_rotates_for_memory_consolidation(self):
        primary = RetryingServiceProvider()
        backup = FakeProvider(result="updated memory")
        backup.consolidate_memory = backup.chat
        pool = FailoverSummarizer([("openai", primary), ("deepseek", backup)])
        with patch("src.summarize.base.time.sleep") as sleep:
            self.assertEqual(pool.consolidate_memory("old", []), "updated memory")
        self.assertEqual((primary.calls, backup.calls), (4, 1))
        self.assertEqual(sleep.call_count, 3)

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

    def test_non_retryable_401_does_not_rotate(self):
        primary = RetryingServiceProvider()
        backup = FakeProvider(result="wrongly rotated")

        def fail():
            primary.calls += 1
            raise AuthenticationFailed("synthetic 401")

        primary.chat = lambda *args, **kwargs: AbstractSummarizer._retry_with_backoff(
            primary, fail, "chat")
        pool = FailoverSummarizer([("first", primary), ("second", backup)])
        with self.assertRaises(AuthenticationFailed):
            pool.chat("question")
        self.assertEqual((primary.calls, backup.calls), (1, 0))


if __name__ == "__main__":
    unittest.main()
