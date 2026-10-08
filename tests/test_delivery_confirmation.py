"""No live WeChat: confirm only synthetic own-account echoes after a baseline."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src.db import MessageStore, initialize_db
from src.wechat.wcdb_backend import WcdbBackend
from src.conversation_policy import DEFAULT_POLICY


def backend(tmp_path):
    result=WcdbBackend.__new__(WcdbBackend)
    result._store=MessageStore(initialize_db(str(tmp_path/'test.db')))
    result._bot_name="Assistant";result._running=True;result._confirmation_lock=threading.Lock();result._client_lock=threading.Lock()
    result._last_confirmed_id={};result._client=Mock();result._client._config={'myWxid':'own'}
    result._talker_to_name=lambda _: 'synthetic group'
    result._send_and_confirm=Mock(return_value=True)
    return result


def test_matching_new_own_echo_records_only_confirmed_body(tmp_path):
    bot=backend(tmp_path)
    old=dict(sender_username='own',server_id='old',local_id=1,message_content='hello',create_time=int(time.time()))
    new=dict(old,server_id='new',local_id=2)
    bot._client.get_messages.side_effect=[[old],[new,old]]
    with patch('src.wechat.wcdb_backend.time.sleep'),patch('src.conversation_policy.load_policy',return_value=DEFAULT_POLICY):
        assert bot.send_confirmed_text('g','hello',action='表态')
    row=bot._store.conn.execute('SELECT * FROM assistant_outbox').fetchone()
    assert row['status']=='confirmed' and row['content']=='hello' and row['action']=='表态'
    bot._store.conn.close()


def test_baseline_or_other_sender_does_not_confirm(tmp_path):
    bot=backend(tmp_path)
    old=dict(sender_username='own',server_id='old',local_id=1,message_content='hello',create_time=int(time.time()))
    other=dict(old,sender_username='other',server_id='new',local_id=2)
    bot._client.get_messages.side_effect=[[old],[old,other]]
    with patch('src.wechat.wcdb_backend.time.sleep'),patch('src.conversation_policy.load_policy',return_value=DEFAULT_POLICY),patch('src.wechat.wcdb_backend.time.monotonic',side_effect=[0,1,20]):
        assert not bot.send_confirmed_text('g','hello')
    row=bot._store.conn.execute('SELECT * FROM assistant_outbox').fetchone()
    assert row['status']=='unconfirmed' and row['content']==''
    bot._send_and_confirm.assert_called_once()
    bot._store.conn.close()
