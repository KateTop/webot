"""End-to-end protocol checks with synthetic data and temporary databases only."""
import datetime as dt
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from src.db import MessageStore, initialize_db
from src.memory.document import *
from src.memory.workspace import MemoryWorkspace, MemoryConflict
from src.memory.consolidator import MemoryConsolidator
from src.summarize.openai_backend import OpenAISummarizer
from src.summarize.claude_backend import ClaudeSummarizer
from src.router import MessageRouter


@pytest.fixture
def workspace(tmp_path):
    store=MessageStore(initialize_db(str(tmp_path/'test.db')))
    result=MemoryWorkspace(store)
    yield result
    store.conn.close()


def entry(subject='alice',section='每个人',text='Alice likes tea'):
    return dict(id=new_id(section=='观察中'),section=section,text=text,subject_id=subject,
        topic='饮食',dates=[],evidence=[])


def test_migration_backup_conflict_and_restore(workspace):
    store=workspace.store
    store.save_group_memory_text('g','legacy synthetic text')
    preview=workspace.preview('g')
    workspace.migrate('g',preview['text'],tuple(preview['token']))
    current,token=workspace.read('g')
    assert parse(current)[0]['text']=='legacy synthetic text'
    assert workspace.snapshots('g')
    with pytest.raises(MemoryConflict):
        workspace.commit('g',current,tuple(preview['token']))
    workspace.restore('g',workspace.snapshots('g')[0]['id'])
    assert parse(workspace.read('g')[0])[0]['text']=='legacy synthetic text'


def test_external_edit_is_detected(workspace):
    text=render([entry()]);_,token=workspace.read('g');workspace.commit('g',text,token)
    _,token=workspace.read('g')
    workspace.store._soul_path('g').write_text(render([entry(text='external changed fact')]),encoding='utf-8')
    with pytest.raises(MemoryConflict): workspace.commit('g',text,token)


def test_forget_permission_snapshots_and_reextraction_block(workspace):
    a,b=entry(),entry('bob',text='Bob likes coffee')
    _,token=workspace.read('g');workspace.commit('g',render([a,b]),token)
    text,token=workspace.read('g');workspace.commit('g',text+'\n',token)
    with pytest.raises(ValueError): workspace.forget('g','alice',[b['id']],'饮食')
    workspace.forget('g','alice',[a['id']],'饮食')
    assert all(e['subject_id']!='alice' for e in parse(workspace.read('g')[0]))
    for row in workspace.store.conn.execute('SELECT text FROM memory_snapshots WHERE chat_id=?',('g',)):
        assert 'Alice likes tea' not in row[0]
    rows=[dict(message_id='e',sender_id='alice',timestamp=100)]
    proposal=json.dumps({'changes':[dict(id=None,section='每个人',subject_id='alice',topic='饮食',text='Alice likes tea',evidence_message_ids=['e'])]})
    with pytest.raises(ValueError): apply_write([],proposal,rows,workspace.blocks('g'))


def test_evidence_dates_come_from_messages_not_model():
    observe=entry(section='观察中')
    rows=[dict(message_id='a',sender_id='alice',timestamp=dt.datetime(2026,10,1).timestamp())]
    proposal={'changes':[dict(id=observe['id'],section='观察中',subject_id='alice',topic='饮食',text='可能喜欢茶',dates=['1900-01-01'],evidence_message_ids=['a'])]}
    result=apply_write([observe],json.dumps(proposal),rows,[])
    assert result[0]['dates']==['2026-10-01']
    proposal['changes'][0]['section']='每个人'
    with pytest.raises(ValueError): apply_write(result,json.dumps(proposal),rows,[])
    rows.extend([dict(message_id='b',sender_id='alice',timestamp=dt.datetime(2026,10,2).timestamp()),dict(message_id='c',sender_id='alice',timestamp=dt.datetime(2026,10,3).timestamp())])
    proposal['changes'][0]['evidence_message_ids']=['a','b','c']
    assert apply_write(result,json.dumps(proposal),rows,[])[0]['id']==observe['id']
    proposal['changes'][0]['evidence_message_ids']=['unknown']
    with pytest.raises(ValueError): apply_write(result,json.dumps(proposal),rows,[])


def test_operations_cannot_invent_or_change_owners():
    a,b=entry(),entry('bob')
    with pytest.raises(ValueError): apply_operations([a,b],json.dumps({'operations':[dict(action='merge',ids=[a['id'],b['id']],excerpts=['Alice likes tea'])]}))
    with pytest.raises(ValueError): apply_operations([a],json.dumps({'operations':[dict(action='compress',ids=[a['id']],excerpts=['invented fact'])]}))
    result,_=apply_operations([a],json.dumps({'operations':[dict(action='compress',ids=[a['id']],excerpts=['likes tea'])]}))
    assert result[0]['text']=='likes tea'


@pytest.mark.parametrize('result',['', 'NO_UPDATE extra','```NO_UPDATE```','{}'])
def test_invalid_result_never_advances_cursor(workspace,result):
    store=workspace.store
    store.insert_message(dict(message_id='e',chat_id='g',sender_id='alice',sender_name='Alice',content='synthetic message',timestamp=100))
    ai=Mock();ai.memory_request.return_value=result
    worker=MemoryConsolidator(store,ai)
    assert not worker.check_and_consolidate('g',force=True)
    assert store.get_group_memory('g') is None


def test_normal_no_update_advances_cursor_once(workspace):
    store=workspace.store
    store.insert_message(dict(message_id='e',chat_id='g',sender_id='alice',sender_name='Alice',content='synthetic message',timestamp=100))
    ai=Mock();ai.memory_request.return_value='NO_UPDATE'
    worker=MemoryConsolidator(store,ai)
    assert worker.check_and_consolidate('g',force=True)
    assert not worker.check_and_consolidate('g',force=True)
    ai.memory_request.assert_called_once()


def test_openai_and_claude_truncation_rejected():
    provider=OpenAISummarizer.__new__(OpenAISummarizer);provider.model='test';provider.disable_thinking=False
    provider.client=Mock();provider.client.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='NO_UPDATE'))])
    with pytest.raises(ValueError): provider._call_protocol_api('synthetic')
    provider=ClaudeSummarizer.__new__(ClaudeSummarizer);provider.model='test';provider.client=Mock()
    provider.client.messages.create.return_value=SimpleNamespace(stop_reason='max_tokens',content=[])
    with pytest.raises(ValueError): provider._call_protocol_api('synthetic')


def test_proactive_action_enforced():
    provider=OpenAISummarizer.__new__(OpenAISummarizer);provider.max_retries=1
    with patch('src.summarize.prompt_settings.load_prompt_settings',return_value={}):
        provider._call_chat_api=lambda *a:json.dumps({'action':'反对','text':'different view'})
        assert provider.proactive_chat(SimpleNamespace(),[{'content':'synthetic discussion'}])==''
        provider._call_chat_api=lambda *a:json.dumps({'action':'追问','text':'why this detail?'})
        assert provider.proactive_chat(SimpleNamespace(),[{'content':'synthetic discussion'}])['action']=='追问'


def test_reminders_require_confirmation(workspace):
    router=MessageRouter.__new__(MessageRouter);router._store=workspace.store;router._proactive=Mock()
    request={'chat_id':'g','authorized_reminder':{'subject_id':'alice','due':time.time()+3600,'text':'synthetic reminder'}}
    router.record_delivery(request,False,'')
    assert workspace.store.conn.execute('SELECT COUNT(*) FROM authorized_reminders').fetchone()[0]==0
    router.record_delivery(request,True,'confirmed-message')
    assert workspace.store.conn.execute('SELECT confirmation_message_id FROM authorized_reminders').fetchone()[0]=='confirmed-message'
    assert router._parse_reminder({'sender_id':'alice'},'提醒我明天喂猫') is None


def test_observation_expiry_and_empty_settling(workspace):
    observed=entry(section='观察中');observed['dates']=['2026-01-01']
    _,token=workspace.read('g');workspace.commit('g',render([observed]),token)
    ai=Mock();worker=MemoryConsolidator(workspace.store,ai)
    assert worker.maintenance('g',now=dt.datetime(2026,10,9,2).timestamp(),force=True)
    assert not parse(workspace.read('g')[0])
    ai.memory_request.assert_not_called()


def test_explicit_remember_bypasses_batch_threshold_without_skipping_history(workspace):
    message=dict(message_id='e',chat_id='g',sender_id='alice',sender_name='Alice',content='记住我喜欢茶',timestamp=100)
    workspace.store.insert_message(message)
    ai=Mock()
    ai.memory_request.return_value=json.dumps({'changes':[dict(id=None,section='每个人',subject_id='alice',topic='饮食',text='Alice explicitly likes tea',kind='fact',evidence_message_ids=['e'])]})
    worker=MemoryConsolidator(workspace.store,ai)
    assert '记下了' in worker.remember_request(message)
    assert parse(workspace.read('g')[0])[0]['subject_id']=='alice'
    assert workspace.store.get_group_memory('g')['last_message_id'] is None
