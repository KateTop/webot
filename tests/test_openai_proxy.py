"""Local OpenAI-compatible endpoints bypass inherited proxy settings."""

from unittest.mock import Mock, patch

from src.summarize.openai_backend import OpenAISummarizer


def test_loopback_api_uses_direct_client_and_remote_api_keeps_default_proxy():
    direct_client = Mock()
    with (patch("src.summarize.openai_backend.DefaultHttpxClient",
                return_value=direct_client) as client_factory,
          patch("src.summarize.openai_backend.OpenAI") as openai_factory):
        OpenAISummarizer(api_key="synthetic", base_url="http://127.0.0.1:8080/v1")
        client_factory.assert_called_once_with(trust_env=False)
        assert openai_factory.call_args.kwargs["http_client"] is direct_client

        client_factory.reset_mock()
        OpenAISummarizer(api_key="synthetic", base_url="https://api.example.com/v1")
        client_factory.assert_not_called()
        assert "http_client" not in openai_factory.call_args.kwargs
