from unittest.mock import patch
from src.summarize.prompt_settings import render_template, with_user_instructions, format_history
from src.memory.instructions import base_prompt, protocol_prompt

def test_single_pass_preserves_json_and_inserted_placeholders():
    assert render_template('{name} {recent} {"text":"x"} {{name}}',{'name':'bot','recent':'literal {name}'}) == 'bot literal {name} {"text":"x"} {{name}}'

def test_shared_base_receives_soul_and_persona_without_recursion():
    settings={'base':'你是{name}；性格{persona}；记忆{soul}；{base}', 'persona':'curious'}
    with patch('src.summarize.prompt_settings.load_prompt_settings',return_value=settings):
        result=base_prompt('Synthetic',{'soul':'synthetic-memory'})
    assert 'Synthetic' in result and 'curious' in result and 'synthetic-memory' in result
    assert '基础人设不能引用自身' in result

def test_custom_chat_values_and_missing_context():
    settings={'chat':'{base} {sender}: {question} {recent} {history} {today} {max_chars}'}
    with patch('src.summarize.prompt_settings.load_prompt_settings',return_value=settings):
        value=with_user_instructions('builtin','chat',{'sender':'Synthetic','question':'question','recent':'recent'})
    assert 'Synthetic: question recent' in value and '本场景未提供' in value
    assert '{today}' not in value and '{max_chars}' not in value

def test_memory_template_and_mandatory_protocol():
    with patch('src.summarize.prompt_settings.load_prompt_settings',return_value={'memory':'{name} {soul} {history} {today} {max_chars} 输出空字符串'}):
        prompt=protocol_prompt('write',[],[{'timestamp':1,'sender_name':'S','sender_id':'synthetic','message_id':3,'content':'text'}],name='Synthetic')
    assert 'Synthetic' in prompt and 'message_id=3: text' in prompt and '【每个人】' in prompt
    assert 'NO_UPDATE' in prompt and '最高优先级输出协议' in prompt
    assert '{history}' not in prompt

def test_real_chat_and_proactive_use_template_values():
    from src.summarize.openai_backend import OpenAISummarizer
    from src.proactive.modes import ProactiveMode
    provider=OpenAISummarizer.__new__(OpenAISummarizer)
    provider.max_retries=1
    provider.retry_exceptions=()
    settings={'chat':'Q={question};R={recent};N={name};S={sender}', 'proactive':'M={my_recent};A={allowed_moves};R={recent}'}
    rows=[{'timestamp':1,'sender_name':'S','sender_id':'synthetic','content':'synthetic context'}]
    captured=[]
    def fake(system,messages):
        captured.append(system)
        return '{"action":"追问","text":"synthetic reply"}'
    provider._call_chat_api=fake
    with patch('src.summarize.prompt_settings.load_prompt_settings',return_value=settings):
        provider.chat('synthetic question',rows,requester_name='Sender',bot_name='Synthetic')
        mode=ProactiveMode('test','test','test','test',0,30,1,80,40)
        result=provider.proactive_chat(mode,rows,participation_feedback='no evidence')
    assert 'Q=synthetic question' in captured[0] and 'N=Synthetic' in captured[0]
    assert 'no evidence' in captured[1] and 'A=追问、带细节的回应' in captured[1]
    assert result['text']=='synthetic reply'
