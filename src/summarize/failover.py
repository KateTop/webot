"""Ordered AI providers; exhausted transient failures advance to the next."""

import logging

from .base import BackendTransientError

logger = logging.getLogger(__name__)


class FailoverSummarizer:
    def __init__(self, providers: list[tuple[str, object]]):
        if not providers:
            raise ValueError("AI pool needs at least one configured provider")
        self.providers = providers

    @property
    def last_api_call_time(self):
        return max(getattr(provider, "last_api_call_time", 0)
                   for _, provider in self.providers)

    def _call(self, method: str, *args, **kwargs):
        for index, (name, provider) in enumerate(self.providers):
            try:
                return getattr(provider, method)(*args, **kwargs)
            except BackendTransientError as error:
                if index == len(self.providers) - 1:
                    raise
                cause = error.__cause__
                status = getattr(cause, "status_code", None)
                detail = f"HTTP {status}" if status else type(cause).__name__
                logger.warning("AI provider %s exhausted %s during %s; trying %s",
                               name, detail, method, self.providers[index + 1][0])

    def chat(self, *args, **kwargs):
        return self._call("chat", *args, **kwargs)

    def proactive_chat(self, *args, **kwargs):
        return self._call("proactive_chat", *args, **kwargs)

    def summarize(self, *args, **kwargs):
        return self._call("summarize", *args, **kwargs)

    def memory_request(self, *args, **kwargs):
        return self._call("memory_request", *args, **kwargs)

    def consolidate_memory(self, *args, **kwargs):
        return self._call("consolidate_memory", *args, **kwargs)

    def format_summary_for_reply(self, *args, **kwargs):
        return self.providers[0][1].format_summary_for_reply(*args, **kwargs)
