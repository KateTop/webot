import json
from unittest.mock import patch, MagicMock
import pytest
from src.wechat.mcp_client import McpWeChatClient, save_connection, load_connection, validate_endpoint
from src.wechat.wcdb_backend import WcdbBackend

CONNECTION={"endpoint":"http://127.0.0.1:13818/mcp","token":"synthetic-token","account":""}

def client():
    c=McpWeChatClient(CONNECTION)
    c._config={"myWxid":"wxid_self"}
    return c

def test_paginated_discovery_and_protocol_notification():
    c=client();calls=[]
    def rpc(method,params=None,notification=False):
        calls.append((method,params,notification))
        if method=='initialize':return {'protocolVersion':'2025-06-18'}
        if method=='notifications/initialized':return {}
        if not params:return {'tools':[{'name':'wechat.core.get_account_info'}],'nextCursor':'next'}
        assert params['cursor']=='next'
        return {'tools':[{'name':'wechat.chat.list_sessions'},{'name':'wechat.chat.get_messages'}]}
    c._rpc=rpc;c.init()
    assert calls[1][2] and len(c.tools)==3

def test_structured_content_preferred_and_account_bound():
    c=client();c._account='synthetic-account'
    captured=[]
    c._rpc=lambda method,params:(captured.append(params) or {'structuredContent':{'status':'success','messages':[]},'content':[{'type':'text','text':'do not parse me'}]})
    assert c._tool('wechat.chat.get_messages')['messages']==[]
    assert captured[0]['arguments']['account']=='synthetic-account'

def test_page_budget_exact_ids_self_and_members():
    c=client();calls=[]
    def tool(name,args):
        calls.append(args)
        count=args['limit']
        return {'source':'realtime','sourceFallback':False,'hasMore':True,'messages':[
            {'id':i,'localId':i,'serverIdStr':'90071992547409931','createTime':100,'senderUsername':'wxid_other','senderDisplayName':'Synthetic','content':'text','type':1}
            for i in range(args['offset'],args['offset']+count)]}
    c._tool=tool
    rows=c.get_messages('g@chatroom',limit=120)
    assert len(rows)==120 and [r['limit'] for r in calls]==[50,50,20]
    assert rows[0]['server_id']=='90071992547409931'
    assert c.get_group_members('g@chatroom')==[{'username':'wxid_other'}]
    own=c._normalize({'isSent':True,'senderUsername':'other','localId':1,'content':'text'},'g')
    assert own['sender_username']=='wxid_self'

def test_reject_old_snapshot_and_nonlocal_endpoint():
    c=client();c._tool=lambda *args:{'source':'decrypted','sourceFallback':True,'messages':[]}
    with pytest.raises(RuntimeError,match='实时'):c.get_messages('g')
    with pytest.raises(ValueError):validate_endpoint('http://example.com/mcp')

def test_structured_mentions_and_quotes_ignore_quoted_at():
    c=client();r=WcdbBackend.__new__(WcdbBackend)
    r._client=c;r._bot_name='Bot';r._store=None;r._voice_config=None
    raw=c._normalize({'localId':1,'createTime':100,'content':'question','type':49,'senderUsername':'wxid_other',
        'quoteUsername':'wxid_self','quoteContent':'old @Bot','atUsernames':[]},'g@chatroom')
    result=r._standardize(raw,'Group','g@chatroom')
    assert result['quotes_bot'] and not result['is_at_mentioned']
    assert result['quoted_content']=='old @Bot' and result['content']=='question'
    raw['mcp_quote_username']='wxid_other';raw['mcp_at_users']=['wxid_self']
    result=r._standardize(raw,'Group','g@chatroom')
    assert not result['quotes_bot'] and result['is_at_mentioned']

def test_mcp_factory_never_constructs_native_reader():
    r=WcdbBackend.__new__(WcdbBackend);r._voice_config=MagicMock(wechat_backend='mcp')
    with patch('src.wechat.mcp_client.McpWeChatClient',return_value='synthetic') as mcp,patch('src.wechat.wcdb_backend.WcdbNativeClient') as native:
        assert r._make_client()=='synthetic'
    native.assert_not_called();mcp.assert_called_once()

def test_config_token_masking_preserves_existing(tmp_path):
    with patch('src.wechat.mcp_client.CONNECTION_FILE',tmp_path/'connection.json'):
        saved=save_connection(CONNECTION)
        assert saved['token']=='********'
        save_connection({'endpoint':CONNECTION['endpoint'],'token':'********'})
        assert load_connection()['token']=='synthetic-token'
        from tests.test_web_api import _build_handler
        _,sock=_build_handler('/api/mcp-config')
        result=json.loads(sock.get_response_text().split('\r\n\r\n',1)[1])
        assert result['config']['token']=='********'
        assert 'synthetic-token' not in sock.get_response_text()
        _,sock=_build_handler('/api/mcp-config',headers={'Origin':'https://example.com'})
        assert not json.loads(sock.get_response_text().split('\r\n\r\n',1)[1])['ok']
