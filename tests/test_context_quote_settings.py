"""Focused regression checks without reading live WeChat content or secrets."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import BotConfig, _validate_config
from src.db.store import MessageStore
from src.router import MessageRouter
from src.summarize import create_summarizer
from src.summarize.openai_backend import OpenAISummarizer
from src.wechat.quote import inspect_quote, parse_quote
from src.wechat.wcdb_backend import WcdbBackend
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


def test_quote_of_another_person_extracts_only_new_mention_text():
    xml = ('<msg><appmsg><type>57</type><title>@机器人 你怎么看？</title>'
           '<refermsg><fromusr>wxid_other</fromusr>'
           '<content>原文</content></refermsg></appmsg></msg>')
    assert parse_quote(xml, 'wxid_bot') == (False, '@机器人 你怎么看？', '')

    backend = WcdbBackend(bot_display_name='机器人', groups=['Group'])
    backend._client = SimpleNamespace(
        _config={'myWxid': 'wxid_bot'}, resolve_nickname=lambda wxid: wxid,
    )
    raw = {
        'sender_username': 'wxid_sender', 'message_content': xml,
        'localType': 49, 'create_time': 123, 'server_id': 'msg-1',
    }
    message = backend._standardize(raw, 'Group', 'g@chatroom')
    assert message['content'] == '@机器人 你怎么看？'
    assert message['is_at_mentioned'] is True
    assert message['quotes_bot'] is False

    # An @mention inside the quoted old text must not trigger a reply.
    raw['message_content'] = xml.replace('@机器人 你怎么看？', '你怎么看？').replace(
        '<content>原文</content>', '<content>@机器人 原文</content>')
    message = backend._standardize(raw, 'Group', 'g@chatroom')
    assert message['content'] == '你怎么看？'
    assert message['is_at_mentioned'] is False


def test_quote_with_encoded_xml_and_identity_mention_in_outer_source():
    xml = ('<msg><appmsg><type> 57 </type><title>继续问</title>'
           '<refermsg><fromusr>wxid_other</fromusr>'
           '<content>旧消息里有@机器人</content></refermsg></appmsg>'
           '<msgsource>&lt;msgsource&gt;&lt;atuserlist&gt;wxid_bot&lt;/atuserlist&gt;'
           '&lt;/msgsource&gt;</msgsource></msg>')
    info = inspect_quote('wxid_sender:\n' + xml, 'wxid_bot')
    assert info.valid and info.mentions_this_account
    assert not info.quotes_this_account
    backend = WcdbBackend(bot_display_name='机器人', groups=['Group'])
    backend._client = SimpleNamespace(
        _config={'myWxid': 'wxid_bot'}, resolve_nickname=lambda wxid: wxid,
    )
    raw = {'sender_username': 'wxid_sender', 'message_content': xml,
           'localType': 49, 'create_time': 123, 'server_id': 'msg-2'}
    message = backend._standardize(raw, 'Group', 'g@chatroom')
    assert message['is_at_mentioned'] is True
    assert message['content'] == '继续问'


def test_quote_of_bot_without_new_title_is_still_routable():
    xml = ('<msg><appmsg><type>57</type><title></title>'
           '<refermsg><fromusr>wxid_bot</fromusr><content>原回答</content>'
           '</refermsg></appmsg></msg>')
    backend = WcdbBackend(bot_display_name='机器人', groups=['Group'])
    backend._client = SimpleNamespace(
        _config={'myWxid': 'wxid_bot'}, resolve_nickname=lambda wxid: wxid,
    )
    raw = {'sender_username': 'wxid_sender', 'message_content': xml,
           'localType': 49, 'create_time': 123, 'server_id': 'msg-3'}
    message = backend._standardize(raw, 'Group', 'g@chatroom')
    assert message['quotes_bot'] is True
    assert message['content'] == '[引用了你的消息]'
    assert message['quoted_content'] == '原回答'


def test_direct_reply_carries_exact_recipient_for_native_mention():
    import time

    router = MessageRouter.__new__(MessageRouter)
    router._config = SimpleNamespace(bot_display_name='Bot', chat_context_count=30,
                                     admin_wxid='')
    router._store = SimpleNamespace(insert_message=lambda msg: True,
                                    get_recent_messages=lambda *a, **kw: [])
    router._memory = SimpleNamespace(check_and_consolidate=lambda chat_id: False)
    router._nicks = SimpleNamespace(resolve_name=lambda wxid: 'Alice',
                                    resolve_wxids=lambda text: text)
    router._summarizer = SimpleNamespace(chat=lambda **kw: '回答')
    router._detector = SimpleNamespace(is_trigger=lambda **kw: False)
    router._sticky = None
    import threading
    router._mention_lock = threading.Lock()
    router._mention_windows = {}
    router._proactive = None
    router._admin = SimpleNamespace(handle=lambda *a: None)
    router.messages_processed = 0
    msg = {'chat_id': 'g', 'group_name': 'G', 'sender_id': 'wxid_alice',
           'sender_name': 'Alice', 'message_id': 'm', 'content': '@Bot 你好',
           'is_at_mentioned': True, 'timestamp': int(time.time())}
    assert router.handle(msg) == '@Alice 回答'
    assert msg['reply_mention'] == ('@Alice ', 'wxid_alice')


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
               return_value={'chat': '使用自定义语气', 'summary': '分点总结', 'proactive': '只追问细节'}):
        provider.chat('你好')
        provider.proactive_chat(
            SimpleNamespace(context_count=1, label='一般', description='一般',
                            instruction='自然回应', max_chars=50),
            [{'sender_name': '甲', 'content': '你好'}],
        )
    assert len(seen) == 2
    assert '使用自定义语气' in seen[0]
    assert '使用自定义语气' not in seen[1]
    assert '只追问细节' in seen[1]
    assert all('分点总结' not in prompt for prompt in seen)


def test_proactive_uses_configured_recent_count():
    seen = {}

    def recent(chat_id, before_ts, limit):
        seen['query'] = (chat_id, before_ts, limit)
        return [{'sender_id': 'u', 'sender_name': 'U', 'content': '当前话题'}]

    def reply(**kwargs):
        seen['reply'] = kwargs
        return '接话'

    router = SimpleNamespace(
        _store=SimpleNamespace(get_recent_messages=recent),
        _config=SimpleNamespace(chat_context_count=23, bot_display_name='Bot'),
        _nicks=SimpleNamespace(resolve_name=lambda sender: sender,
                               resolve_wxids=lambda text: text),
        _summarizer=SimpleNamespace(proactive_chat=reply),
        _proactive=SimpleNamespace(record_speech=lambda chat_id: None),
    )
    mode = SimpleNamespace(name='CASUAL')
    assert MessageRouter._handle_proactive_chat(router, {'chat_id': 'g', 'timestamp': 123}, mode) == '接话'
    assert seen['query'] == ('g', 123, 40)
    assert seen['reply']['group_memory'] == ''


def test_chat_loads_group_memory_only_after_model_requests_it():
    seen = []
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        return '[[NEED_GROUP_MEMORY]]' if len(calls) == 1 else '根据旧记录回答'

    router = MessageRouter.__new__(MessageRouter)
    router._store = SimpleNamespace(
            get_recent_messages=lambda *args, **kwargs: [],
            get_group_memory=lambda chat_id: seen.append(chat_id) or
                {'memory_text': '分群旧记录'},
        )
    router._config = SimpleNamespace(chat_context_count=30, bot_display_name='Bot')
    router._nicks = SimpleNamespace(resolve_name=lambda sender: sender,
                                    resolve_wxids=lambda text: text)
    router._summarizer = SimpleNamespace(chat=chat)
    msg = {'chat_id': 'g', 'sender_id': 'u', 'sender_name': 'U',
           'message_id': 'm', 'timestamp': 123}
    assert MessageRouter._handle_chat(router, msg, '他以前怎样？') == '@U 根据旧记录回答'
    assert seen == ['g']
    assert [call['group_memory'] for call in calls] == ['', '分群旧记录']


def test_chat_does_not_load_memory_for_normal_reply():
    def forbidden(chat_id):
        raise AssertionError('long-term memory should not be loaded')

    router = MessageRouter.__new__(MessageRouter)
    router._store = SimpleNamespace(get_recent_messages=lambda *args, **kwargs: [],
                                    get_group_memory=forbidden)
    router._config = SimpleNamespace(chat_context_count=30, bot_display_name='Bot')
    router._nicks = SimpleNamespace(resolve_name=lambda sender: sender,
                                    resolve_wxids=lambda text: text)
    router._summarizer = SimpleNamespace(chat=lambda **kwargs: '直接回答')
    msg = {'chat_id': 'g', 'sender_id': 'u', 'sender_name': 'U',
           'message_id': 'm', 'timestamp': 123}
    assert MessageRouter._handle_chat(router, msg, '你好') == '@U 直接回答'


@pytest.mark.parametrize('field,value', [('ai_retry_count', -1),
                                          ('ai_retry_count', 11),
                                          ('chat_context_count', 0),
                                          ('chat_context_count', 101)])
def test_numeric_settings_reject_bad_values(field, value):
    with pytest.raises(RuntimeError):
        _validate_config({field: value})
    with pytest.raises(ValueError):
        _validate_ai_numeric_settings({field: value})
