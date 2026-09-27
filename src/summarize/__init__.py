"""Summarization module — factory for AI backends.

Usage:
    from .summarize import create_summarizer

    summarizer = create_summarizer(config)
    result = summarizer.summarize(messages, requester_name)
"""

import logging

from .base import AbstractSummarizer
from .claude_backend import ClaudeSummarizer
from .deepseek_backend import DeepSeekSummarizer
from .openai_backend import OpenAISummarizer
from .models import ParticipantContribution, SummaryResult
from .failover import FailoverSummarizer

logger = logging.getLogger(__name__)

__all__ = [
    "AbstractSummarizer",
    "ClaudeSummarizer",
    "DeepSeekSummarizer",
    "OpenAISummarizer",
    "SummaryResult",
    "ParticipantContribution",
    "create_summarizer",
]


def create_summarizer(config) -> AbstractSummarizer:
    """Create the appropriate summarizer based on config.ai_backend.

    Args:
        config: BotConfig instance with ai_backend, api keys, model, etc.

    Returns:
        An AbstractSummarizer implementation (Claude, DeepSeek, or OpenAI).

    Raises:
        ValueError: If the configured backend is unknown.
    """
    order = [config.ai_backend.lower()]
    for name in getattr(config, "fallback_backends", []):
        name = name.lower()
        if name not in order:
            order.append(name)
    providers = []
    for name in order:
        key = {"deepseek": config.deepseek_api_key,
               "openai": config.openai_api_key,
               "claude": config.anthropic_api_key,
               "custom": config.custom_api_key}.get(name)
        if name not in ("deepseek", "openai", "claude", "custom"):
            raise ValueError(f"Unknown AI_BACKEND: {name}")
        if not key and name != order[0]:
            logger.warning("Skipping unconfigured AI fallback: %s", name)
            continue
        provider = _create_single(config, name, len(order) > 1)
        provider.max_retries = getattr(config, "ai_retry_count", 3) + 1
        providers.append((name, provider))
    return providers[0][1] if len(providers) == 1 else FailoverSummarizer(providers)


def _create_single(config, backend: str, pool_mode: bool) -> AbstractSummarizer:

    if backend == "deepseek":
        logger.info("Creating DeepSeekSummarizer (model=%s)", config.deepseek_model)
        return DeepSeekSummarizer(
            api_key=config.deepseek_api_key,
            model=config.deepseek_model,
            base_url=config.deepseek_base_url,
            chunk_size=config.chunk_size,
        )

    elif backend == "claude":
        logger.info("Creating ClaudeSummarizer (model=%s)", config.summarize_model)
        return ClaudeSummarizer(
            api_key=config.anthropic_api_key,
            model=config.summarize_model,
            base_url=config.anthropic_base_url,
            chunk_size=config.chunk_size,
        )

    elif backend == "openai":
        logger.info("Creating OpenAISummarizer (model=%s)", config.openai_model)
        return OpenAISummarizer(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.openai_base_url,
            chunk_size=config.chunk_size,
            web_search=config.openai_web_search,
        )

    elif backend == "custom":
        if not config.custom_base_url or not config.custom_model:
            raise ValueError("Custom backend requires base URL and model")
        return OpenAISummarizer(
            api_key=config.custom_api_key,
            model=config.custom_model,
            base_url=config.custom_base_url,
            chunk_size=config.chunk_size,
        )

    else:
        raise ValueError(
            f"Unknown AI_BACKEND: '{config.ai_backend}'. "
            f"Supported: 'claude', 'deepseek', 'openai'."
        )
