"""Focused regression checks without reading live WeChat content or secrets."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import BotConfig, _validate_config
from src.db.store import MessageStore
from src.summarize import create_summarizer
from src.summarize.openai_backend import OpenAISummarizer
from src.wechat.quote import parse_quote
from src.web.server import _validate_ai_numeric_settings


def test_recent_messages_are_group_scoped_and_before_trigger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (message_id TEXT, chat_id TEXT, sender_id TEXT, "
                 "sender_name TEXT, content TEXT, msg_type INTEGER, timestamp INTEGER)")
    for n in range(40):
        conn.execute("INSERT INTO messages VALUES (?, 'g', 'u', 'U', ?, 1, ?)",
                     (str(n), str(n), 100 + n))
    conn.execute("INSERT INTO messages VALUES ('trigger', 'g', 'u', 'U', 'ask', 1, 139)")
    conn.execute("INSERT INTO messages VALUES ('later', 'g', 'u', 'U', 'later', 1, 139)")
    conn.execute("INSERT INTO messages VALUES ('other', 'else', 'u', 'U', 'other', 1, 139)")
    rows = MessageStore(conn).get_recent_messages('g', 139, limit=30, exclude_id='trigger')
    assert [r['message_id'] for r in rows] == [str(i) for i in range(10, 40)]


def test_quote_requires_exact_own_wxid():
    xml = ('<msg><appmsg><type>57</type><title>那你怎么看？</title>'
           '<refermsg><fromusr>wxid_bot</fromusr><content>之前的回答</content>'
           '</refermsg></appmsg></msg>')
    assert parse_quote(xml, 'wxid_bot') == (True, '那你怎么看？', '之前的回答')
    assert parse_quote(xml, 'wxid_other')[0] is False
    assert parse_quote('<msg><appmsg><type>57</type>', 'wxid_bot')[0] is False
    assert parse_quote(xml.replace('<type>57</type>', '<type>5</type>'), 'wxid_bot')[0] is False


def test_custom_provider_and_retry_count():
    config = BotConfig(ai_backend='custom', custom_api_key='test',
                       custom_base_url='http://localhost:8080/v1',
                       custom_model='my-model', ai_retry_count=2)
    provider = create_summarizer(config)
    assert (provider.model, provider.max_retries) == ('my-model', 3)


def test_chat_instruction_reaches_proactive_and_direct_reply():
    provider = OpenAISummarizer.__new__(OpenAISummarizer)
    provider.max_retries = 1
    seen = []
    provider._call_chat_api = lambda prompt, messages: seen.append(prompt) or 'ok'
    with patch('src.summarize.prompt_settings.load_prompt_settings',
               return_value={'chat': '使用自定义语气', 'summary': '分点总结'}):
        provider.chat('你好')
        provider.proactive_chat(
            SimpleNamespace(context_count=1, label='一般', description='一般',
                            instruction='自然回应', max_chars=50),
            [{'sender_name': '甲', 'content': '你好'}],
        )
    assert len(seen) == 2
    assert all('使用自定义语气' in prompt for prompt in seen)
    assert all('分点总结' not in prompt for prompt in seen)


@pytest.mark.parametrize('field,value', [('ai_retry_count', -1),
                                          ('ai_retry_count', 11),
                                          ('chat_context_count', 0),
                                          ('chat_context_count', 101)])
def test_numeric_settings_reject_bad_values(field, value):
    with pytest.raises(RuntimeError):
        _validate_config({field: value})
    with pytest.raises(ValueError):
        _validate_ai_numeric_settings({field: value})
