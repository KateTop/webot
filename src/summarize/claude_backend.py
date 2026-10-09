"""Claude (Anthropic) summarization backend.

Uses the Anthropic Python SDK with native structured output (Pydantic parse).
"""

import logging
import time

import anthropic

from .base import AbstractSummarizer
from .prompt_settings import with_user_instructions
from .models import SummaryResult
from .prompts import (
    SYSTEM_PROMPT,
    CHUNK_SYSTEM_PROMPT,
    MERGE_SYSTEM_PROMPT,
    MEMORY_CONSOLE_PROMPT,
    build_summary_prompt,
    build_chunk_summary_prompt,
    build_merge_prompt,
)

logger = logging.getLogger(__name__)


class ClaudeSummarizer(AbstractSummarizer):
    """Summarization via Anthropic Claude API.

    Features:
    - Native structured output via client.messages.parse() + Pydantic
    - Token budget: 150K (safe margin below 200K context window)
    - Map-Reduce chunking for large conversations
    """

    # Claude-specific constants
    MODEL_HAIKU = "claude-haiku-4-5-20251001"
    MODEL_SONNET = "claude-sonnet-4-5-20250929"

    # 200K context window → 150K safe budget
    token_budget = 150_000

    retry_exceptions = (
        anthropic.RateLimitError,
        anthropic.APIConnectionError,
        anthropic.InternalServerError,
        # HTTP 529 Overloaded — not publicly exported by all SDK versions;
        # fall back to InternalServerError (also 5xx) when unavailable.
        getattr(anthropic, "OverloadedError", anthropic.InternalServerError),
    )

    def __init__(self, api_key: str,
                 model: str = MODEL_HAIKU,
                 base_url: str = "https://api.anthropic.com",
                 chunk_size: int = 400,
                 max_retries: int = 3):
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                          timeout=25.0, max_retries=0)
        self.model = model
        self.chunk_size = chunk_size
        self.max_retries = max_retries

    # ── Conversational chat API call (called by base class) ─────

    def _call_chat_api(self, system_prompt: str,
                        messages: list[dict]) -> str:
        """Claude-specific: uses client.messages.create() with system param."""
        response = self._monitored_create(self.client.messages.create,
            model=self.model,
            max_tokens=400,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text or "..."

    # ── Direct summarization ──────────────────────────────────────

    def _summarize_direct(self, messages: list[dict],
                           requester_name: str) -> SummaryResult:
        """All messages in one call — uses Pydantic parse for structured output."""
        user_prompt = build_summary_prompt(messages, requester_name)

        def call():
            response = self._monitored_create(self.client.messages.parse,
                model=self.model,
                max_tokens=8192,
                system=with_user_instructions(SYSTEM_PROMPT, "summary", {"name":getattr(self, "bot_name", "群聊小助手"), "history":user_prompt}),
                messages=[{"role": "user", "content": user_prompt}],
                output_format=SummaryResult,
            )
            return response.parsed_output

        return self._retry_with_backoff(call, "direct summarization")

    # ── Map-Reduce ────────────────────────────────────────────────

    def _summarize_chunk(self, chunk: list[dict], chunk_num: int,
                          total: int, requester_name: str) -> str:
        """Extract key facts from a single chunk (plain text output)."""
        user_prompt = build_chunk_summary_prompt(
            chunk, chunk_num, total, requester_name
        )

        def call():
            response = self._monitored_create(self.client.messages.create,
                model=self.model,
                max_tokens=1024,
                system=with_user_instructions(CHUNK_SYSTEM_PROMPT, "summary", {"name":getattr(self, "bot_name", "群聊小助手"), "history":user_prompt}),
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text

        return self._retry_with_backoff(call, f"chunk {chunk_num}/{total}")

    def _merge_chunk_summaries(self, chunk_summaries: list[str],
                                requester_name: str) -> SummaryResult:
        """Merge chunk summaries into final structured result."""
        user_prompt = build_merge_prompt(chunk_summaries, requester_name)

        def call():
            response = self._monitored_create(self.client.messages.parse,
                model=self.model,
                max_tokens=8192,
                system=with_user_instructions(MERGE_SYSTEM_PROMPT, "summary", {"name":getattr(self, "bot_name", "群聊小助手"), "history":user_prompt}),
                messages=[{"role": "user", "content": user_prompt}],
                output_format=SummaryResult,
            )
            return response.parsed_output

        return self._retry_with_backoff(call, "merge chunk summaries")

    # ── Memory consolidation (Claude backend) ───────────────────────

    def consolidate_memory(self, existing_memory: str, new_messages: list[dict]) -> str:
        from src.memory.document import parse
        return self.memory_request("write", parse(existing_memory), new_messages)

    def _call_protocol_api(self, prompt):
        response = self._monitored_create(self.client.messages.create,model=self.model, max_tokens=8192, system=prompt,
            messages=[{"role": "user", "content": "请按协议返回结果"}])
        if response.stop_reason != "end_turn":
            raise ValueError("记忆响应未正常结束，拒绝推进进度")
        return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
