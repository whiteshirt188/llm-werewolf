"""Official Qwen transport and real-game wiring, exclusively local fake responses."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import httpx
import pytest
from test_api import submit_body
from test_context_and_direct_start import admission, start_body
from test_deepseek import envelope
from test_opening import wait_for
from test_phase_limits import decision
from test_qualification import environment
from test_real_games import GameDouble
from test_table_plan import plan

from werewolf import deepseek, qwen
from werewolf.agent import ModelUsage, PlayerAgent
from werewolf.deepseek import ProviderError
from werewolf.demo import simulated_action
from werewolf.engine import GameEngine
from werewolf.player_state import PLAYER_PROTOCOL
from werewolf.profiles import GenerationParams, ModelConnection, SeatProfile, canonical_json
from werewolf.prompts import PromptBuilder
from werewolf.providers import adapter_version, build_adapter, validate_connection
from werewolf.rulebook import Rulebook


def connection(mode='low', model=qwen.DEFAULT_MODEL):
    generation=GenerationParams(thinking_mode=mode)
    return ModelConnection(id='chosen',provider='dashscope',model=model,base_url=qwen.OFFICIAL_URL,
        generation=generation,adapter_version=adapter_version('dashscope',model,generation.model_dump()))


def request_for(kind='speak', role='villager', day=1, mode='low', model=qwen.DEFAULT_MODEL):
    _,profile,view,_=decision(kind,role,provider='deepseek')
    view.update(day=day,checks=[],wolf_teammates=[])
    c=connection(mode,model)
    builder=PromptBuilder(Rulebook.load(),protocol=PLAYER_PROTOCOL)
    return builder.build(profile,c,view),profile,view,builder


@pytest.mark.parametrize('kind,role', [('propose_kill','wolf'),('kill','wolf'),('inspect','seer'),
    ('witch','witch'),('protect','guard'),('speak','villager'),('vote','villager'),('last_words','hunter')])
@pytest.mark.parametrize('day',[1,2])
def test_same_round_role_total_budget_as_deepseek(kind,role,day):
    request,profile,view,builder=request_for(kind,role,day)
    c=request.connection.model_copy(update={'provider':'deepseek','model':'deepseek-flash',
        'base_url':deepseek.OFFICIAL_URL,'adapter_version':deepseek.THINKING_ADAPTER_VERSION})
    original=builder.build(profile,c,view)
    assert request.max_output_tokens==original.max_output_tokens
    expected=4096*(2 if role in {'seer','witch','hunter','guard','knight'} else 1)*(2 if day==2 else 1)
    assert request.max_output_tokens==min(expected,16384)
    payload=qwen.QwenAdapter('offline-key').payload(request)
    assert payload['max_completion_tokens']==request.max_output_tokens and payload['enable_thinking'] is True
    assert not {'max_tokens','thinking','reasoning_effort','thinking_budget'} & payload.keys()
    assert request.timeout_multiplier==original.timeout_multiplier


@pytest.mark.parametrize('mode',['disabled','low'])
def test_opening_and_smaller_user_limit_keep_existing_policy(mode):
    c=connection(mode);game=GameEngine(38);profile=SeatProfile(seat=1,connection_id=c.id)
    builder=PromptBuilder(Rulebook.load(),protocol=PLAYER_PROTOCOL)
    request=builder.build_opening(profile,c,game.player_view(1))
    assert request.max_output_tokens==(4096 if mode=='low' else 256)
    if mode=='disabled':
        c=c.model_copy(update={'generation':GenerationParams(max_output_tokens=128,target_output_tokens=64)})
        assert builder.build_opening(profile,c,game.player_view(1)).max_output_tokens==64
    assert build_adapter(c,'offline-key')._timeout==(60 if mode=='low' else 30)


@pytest.mark.parametrize('model,mode,json_mode',[(qwen.DEFAULT_MODEL,'disabled',True),
    (qwen.DEFAULT_MODEL,'low',False),('qwen3.8-max','low',True),('qwen-plus','disabled',True)])
def test_model_capabilities_and_provider_specific_payload(model,mode,json_mode):
    request,*_=request_for(mode=mode,model=model)
    payload=qwen.QwenAdapter('offline-key').payload(request)
    assert ('response_format' in payload)==json_mode
    assert ('temperature' in payload)==(mode=='disabled')
    assert payload['stream'] is False and payload['model']==model
    assert ('max_tokens' in payload)==(model=='qwen-plus')
    assert 'thinking' not in payload
    if model=='qwen3.8-max':assert payload['preserve_thinking'] is False
    if json_mode:assert payload['response_format']=={'type':'json_object'}
    legacy=connection(mode,model).model_copy(update={'base_url':qwen.LEGACY_URL})
    validate_connection(legacy)


@pytest.mark.parametrize('changes,code',[
    ({'base_url':'https://evil.invalid'},'unsupported_connection'),
    ({'model':'qwen-unknown'},'unsupported_qwen_model'),
    ({'model':'qwen-plus'},'unsupported_qwen_thinking_model'),
    ({'generation':GenerationParams(reasoning_budget=512)},'unsupported_reasoning_budget'),
    ({'generation':GenerationParams(max_output_tokens=32769)},'invalid_output_limit')])
def test_unsupported_config_is_rejected_before_transport(changes,code):
    with pytest.raises(ProviderError,match=code):validate_connection(connection().model_copy(update=changes))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['disabled','low'])
async def test_exact_http_origin_usage_and_reasoning_never_saved(mode):
    request,profile,view,builder=request_for(mode=mode)
    sent=[]
    def handler(req):
        assert str(req.url)==qwen.OFFICIAL_URL+'/chat/completions'
        assert req.headers['authorization']=='Bearer fake-qwen-key'
        body=json.loads(req.content);sent.append(body)
        assert body['enable_thinking']==(mode=='low') and 'fake-qwen-key' not in canonical_json(body)
        return httpx.Response(200,json=envelope(content=canonical_json({
            'action':{'kind':'speak','text':'我先听听2号。'},'player_update':{'intent':'先听2号'}}),
            usage={'prompt_tokens':100,'completion_tokens':1500,'prompt_tokens_details':{'cached_tokens':50},
                   'completion_tokens_details':{'reasoning_tokens':1400 if mode=='low' else 0}}))
    agent=PlayerAgent(profile,request.connection,builder)
    await agent.decide(view,qwen.QwenAdapter('fake-qwen-key',transport=httpx.MockTransport(handler),simulated=True))
    record=agent.records[-1]
    assert len(sent)==1 and record.thinking_mode==('requested_low' if mode=='low' else 'requested_disabled')
    assert record.usage==ModelUsage(100,1500,50,1400 if mode=='low' else 0)
    assert 'reasoning-canary' not in repr(record) and 'fake-qwen-key' not in repr(record)
    assert record.player_update['intent']=='先听2号'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,code',[('auth','authentication'),('length','truncated_response'),('timeout','timeout')])
async def test_failure_is_one_bounded_request_with_safe_diagnostics(failure,code):
    request,*_=request_for(role='seer',day=2);sent=[]
    def handler(req):
        sent.append(req)
        assert req.extensions['timeout']['read']==120
        if failure=='timeout':raise httpx.ReadTimeout('secret-canary',request=req)
        if failure=='auth':return httpx.Response(401,text='secret-canary')
        data=envelope(usage={'prompt_tokens':100,'completion_tokens':16384,
            'completion_tokens_details':{'reasoning_tokens':16384}})
        data['choices'][0]['finish_reason']='length'
        return httpx.Response(200,json=data)
    with pytest.raises(ProviderError) as error:
        await qwen.QwenAdapter('offline-key',transport=httpx.MockTransport(handler),timeout=60).complete(request)
    assert len(sent)==1 and error.value.code==code and 'secret-canary' not in str(error.value)
    if failure=='length':assert error.value.usage.output_tokens==16384


async def save_qwen(client,headers,mode='low',model=qwen.DEFAULT_MODEL,cid=None):
    response=await client.post('/api/account/connections'+(f'/{cid}' if cid else ''),headers=headers,
        json={'label':'千问','provider':'dashscope','model':model,
              'generation':GenerationParams(thinking_mode=mode).model_dump(),
              **({'api_key':'fake-canary-key-never-use-real'} if not cid else {})})
    assert response.status_code in {200,201},response.text
    assert response.json()['base_url']==qwen.OFFICIAL_URL
    return response.json()['id']


@pytest.mark.asyncio
async def test_free_check_stale_version_and_mixed_game_use_actual_adapters(tmp_path,monkeypatch):
    sent=[]
    originals={'dashscope':qwen.QwenAdapter,'deepseek':deepseek.DeepSeekAdapter}
    def factory(provider):
        def handler(req):
            body=json.loads(req.content);data=json.loads(body['messages'][-1]['content'])
            state=data['state'];opening=data.get('task')=='opening_plan'
            kind='opening_plan' if opening else state['legal_actions'][0]['kind']
            sent.append((provider,body['model'],state['seat'],kind))
            if provider=='dashscope':
                assert 'max_completion_tokens' in body and 'thinking' not in body
                assert body['enable_thinking'] is True
            if opening:content={'intent':'保留自己的判断，先听发言'}
            else:
                action=simulated_action(state);spec=state['legal_actions'][0]
                values=asdict(action);keys=['kind']
                if 'targets' in spec or kind=='witch':keys.append('target')
                if kind=='witch':keys.append('potion')
                if 'max_length' in spec:keys.append('text')
                content={'action':{k:values[k] for k in keys},'player_update':{}}
            return httpx.Response(200,json=envelope(content=canonical_json(content),usage={'prompt_tokens':100,
                'completion_tokens':200,'completion_tokens_details':{'reasoning_tokens':150 if provider=='dashscope' else 0}}))
        return lambda credential,**opts:originals[provider](credential,transport=httpx.MockTransport(handler),simulated=True,**opts)
    monkeypatch.setattr(qwen,'QwenAdapter',factory('dashscope'))
    monkeypatch.setattr(deepseek,'DeepSeekAdapter',factory('deepseek'))
    async with environment(tmp_path,qualification_enabled=False,opening_enabled=True,reading_scale=0,game_retry_delay=0) as (_app,client,_,headers,deep_id,exam):
        qid=await save_qwen(client,headers);table=plan(qid);table['playback_mode']='auto'
        for p in table['ai_profiles']:
            if p['seat']%2==0:p['connection_id']=deep_id
        report=await admission(client,headers,table)
        assert report['all_ready'] and report['model_calls']==0 and not sent
        limits=next(s['thinking_limits'] for s in report['seats'] if s['provider']=='dashscope')
        assert limits=={'opening':4096,'first_regular':4096,'first_god':8192,'later_regular':8192,'later_god':16384}
        body=start_body(table,report)
        assert (await client.post('/api/account/games',headers=headers,json={**body,'confirm_cost':False})).status_code==422
        await save_qwen(client,headers,'disabled',cid=qid)
        assert (await client.post('/api/account/games',headers=headers,json=body)).status_code==409
        assert not sent
        await save_qwen(client,headers,cid=qid)
        body=start_body(table,await admission(client,headers,table))
        created=await client.post('/api/account/games',headers=headers,json=body)
        assert created.status_code==201,created.text
        gid=created.json()['game']['game_id']
        for _ in range(100):
            async with asyncio.timeout(5):
                while True:
                    state=(await client.get(f'/api/games/{gid}')).json()
                    if state['status']!='running' or state['turn_version']:break
                    await asyncio.sleep(.001)
            if state['status']=='finished':break
            assert state['status']=='running',state['pause_reason']
            assert (await client.post(f'/api/games/{gid}/actions',json=submit_body(state))).status_code==200
        else:pytest.fail('offline mixed game did not finish')
        assert {p for p,_,_,_ in sent}=={'dashscope','deepseek'}
        assert sum(kind=='opening_plan' for *_,kind in sent)==5
        for provider,model,seat,_ in sent:
            assert (provider,model)==('deepseek','fixed-chosen-model') if seat%2==0 else (provider,model)==('dashscope',qwen.DEFAULT_MODEL)
        before=len(sent)
        assert (await client.post('/api/account/games',headers=headers,json=body)).json()['game']['game_id']==gid
        await client.get(f'/api/games/{gid}/replay')
        assert (await client.get(f'/api/games/{gid}/usage')).json()['calls']==len(sent)==before
        assert not exam.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy',['more_tokens','without_thinking'])
async def test_qwen_truncation_recovery_preserves_frozen_connection(tmp_path,strategy):
    def fail(n):
        if n==1:raise ProviderError('truncated_response',retryable=True,usage=ModelUsage(100,4096,None,4096))
    double=GameDouble(fail=fail)
    async with environment(tmp_path,qualification_enabled=False,game_factory=double,reading_scale=0,game_retry_delay=0) as (app,client,_,headers,_,_):
        qid=await save_qwen(client,headers);table=plan(qid)
        table['human_seat']=4
        table['ai_profiles']=[{'seat':s,'connection_id':qid} for s in (1,2,3,5,6)]
        created=await client.post('/api/account/games',headers=headers,json=start_body(table,await admission(client,headers,table)))
        assert created.status_code==201,created.text
        gid=created.json()['game']['game_id'];runtime=app.state.service.games[gid]
        await wait_for(lambda:runtime.status=='paused' and runtime.worker is None)
        frozen=deepcopy(runtime.connections);engine=runtime.engine.snapshot()
        assert len(double.calls)==1 and runtime.decision_recovery.increased_limit==8192
        assert (await client.post(f'/api/games/{gid}/resume',json={'operation_id':'unchanged'})).status_code==409
        response=await client.post(f'/api/games/{gid}/resume',json={'operation_id':'recover','recovery':strategy})
        assert response.status_code==200,response.text
        await wait_for(lambda:runtime.prepared is not None)
        assert len(double.calls)==2 and runtime.connections==frozen and runtime.engine.snapshot()==engine
        req=double.calls[-1];body=qwen.QwenAdapter('offline-key').payload(req)
        assert body['enable_thinking']==(strategy=='more_tokens')
        assert req.max_output_tokens==(8192 if strategy=='more_tokens' else 512)
        assert req.connection.adapter_version==(qwen.THINKING_ADAPTER_VERSION if strategy=='more_tokens' else qwen.ADAPTER_VERSION)
        assert runtime.prepared['seat']==double.calls[-1].seat
        rows=app.state.service.store.attempts(gid)
        assert rows[0]['usage']['reasoning_tokens']==4096 and rows[1]['recovery_strategy']==strategy
        assert rows[1]['provider']=='dashscope'
