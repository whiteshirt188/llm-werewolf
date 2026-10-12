"""Chronology and larger round/role budgets, verified without provider calls."""

import json
from copy import deepcopy

import httpx
import pytest
from test_accounts import register
from test_api import opened
from test_context_and_direct_start import admission, start_body
from test_deepseek_thinking import ThinkingDouble, save, table
from test_opening import wait_for
from test_phase_limits import decision

from werewolf.agent import ModelUsage
from werewolf.api import create_app
from werewolf.decision_budget import V10_BUDGET_POLICY
from werewolf.deepseek import DeepSeekAdapter, ProviderError
from werewolf.player_state import PLAYER_PROTOCOL, PlayerState
from werewolf.profiles import GenerationParams, ModelConnection, SeatProfile
from werewolf.prompts import DOCS_PLAYER_PROMPT_VERSION, PLAYER_PROMPT_VERSION, ContextPolicy, PromptBuilder
from werewolf.rulebook import Rulebook
from werewolf.siliconflow import SiliconFlowAdapter


def request_for(kind, role, day=1, *, base=4096, provider='deepseek', version=PLAYER_PROMPT_VERSION,
                override=None, state=None, generation=None):
    _, profile, view, _ = decision(kind, role, provider=provider,
        generation=generation or GenerationParams(thinking_mode='low' if provider == 'deepseek' else 'disabled',
                                                  thinking_output_tokens=base))
    view['day'] = day
    view['checks'] = []
    view['wolf_teammates'] = []
    builder = PromptBuilder(Rulebook.load(),protocol=PLAYER_PROTOCOL,prompt_version=version,
                            output_override=override,player_state=state)
    connection = ModelConnection(id=profile.connection_id, provider=provider,model='deepseek-flash'
        if provider == 'deepseek' else 'zai-org/GLM-5.3',base_url='https://api.deepseek.com/v1'
        if provider == 'deepseek' else 'https://api.siliconflow.cn/v1',
        generation=generation or GenerationParams(thinking_mode='low' if provider == 'deepseek' else 'disabled',
                                                  thinking_output_tokens=base))
    return builder.build(profile,connection,view), builder, profile, connection, view


@pytest.mark.parametrize('kind,role', [
    ('speak','villager'),('vote','villager'),('last_words','villager'),
    ('propose_kill','wolf'),('wolf_message','wolf'),('kill','wolf'),
    ('inspect','seer'),('witch','witch'),('protect','guard'),('duel','knight'),('shoot','hunter'),
    ('speak','seer'),('vote','witch'),('last_words','knight'),
])
@pytest.mark.parametrize('day', [1,2,3])
def test_every_action_uses_true_role_and_actual_round(kind, role, day):
    request, *_ = request_for(kind,role,day,state=PlayerState(claim='wolf' if role in V10_BUDGET_POLICY.gods else 'seer'))
    expected = (8192 if day >= 2 else 4096) * (2 if role in V10_BUDGET_POLICY.gods else 1)
    assert request.max_output_tokens == expected
    body = DeepSeekAdapter('offline-key').payload(request)
    assert body['max_tokens'] == expected and body['thinking'] == {'type':'enabled'}
    assert request.timeout_multiplier == (4 if expected > 8192 else None)
    action = json.loads(request.response_schema)['properties']['action']
    if kind in {'speak','last_words'}:
        assert action['properties']['text']['maxLength'] == 300
    elif kind in {'propose_kill','wolf_message'}:
        assert action['properties']['text']['maxLength'] == 150


@pytest.mark.parametrize('base', [1024,2048,8192,16384])
def test_saved_base_is_scaled_with_a_ceiling(base):
    request, *_ = request_for('witch','witch',2,base=base)
    assert request.max_output_tokens == min(base*4,16384)


def test_explicit_recovery_cap_is_not_multiplied_again():
    normal, *_ = request_for('witch','witch',2,base=1024)
    recovered, *_ = request_for('witch','witch',2,base=8192,override=8192)
    assert normal.max_output_tokens == 4096 and recovered.max_output_tokens == 8192
    assert recovered.timeout_multiplier is None
    assert normal.configuration_fingerprint != recovered.configuration_fingerprint


@pytest.mark.parametrize('role,kind', [('wolf','kill'),('seer','inspect'),('witch','speak'),('villager','vote')])
def test_v9_requests_stay_at_frozen_limits_without_chronology_or_new_timeout(role,kind):
    old, *_ = request_for(kind,role,2,version=DOCS_PLAYER_PROMPT_VERSION)
    new, *_ = request_for(kind,role,2)
    assert old.max_output_tokens == 4096 and old.timeout_multiplier is None
    assert 'turn_context' not in json.loads(old.messages[1].content)
    assert '未发生的发言' not in old.messages[0].content
    assert old.configuration_fingerprint != new.configuration_fingerprint


@pytest.mark.parametrize('role,kind,day,expected', [
    ('wolf','propose_kill',1,512),('wolf','propose_kill',2,1024),
    ('seer','inspect',1,1024),('seer','inspect',2,2048),
    ('witch','speak',1,2048),('witch','speak',2,4096),
])
def test_siliconflow_reasoning_budget_grows_without_enabling_thinking(role,kind,day,expected):
    request, *_ = request_for(kind,role,day,provider='siliconflow')
    body = SiliconFlowAdapter('offline-key').payload(request)
    assert body['thinking_budget'] == expected and body['enable_thinking'] is False
    assert request.timeout_multiplier is None


def test_disabled_mode_keeps_explicit_saved_output_caps():
    for provider in ['deepseek','siliconflow']:
        request, *_ = request_for('speak','seer',2,provider=provider,
                                 generation=GenerationParams(max_output_tokens=128,target_output_tokens=64))
        assert request.max_output_tokens == 128


def test_private_check_and_public_speech_have_distinct_sources_and_dates():
    _, builder, _, connection, view = request_for('speak','seer')
    view.update(seat=5,checks=[{'day':1,'target':7,'is_wolf':False}],
                players=[{'seat':s,'alive':True} for s in range(1,10)],
                events=[{'id':1,'day':1,'kind':'inspection','data':{'target':7,'is_wolf':False}},
                        *[{'id':s+1,'day':1,'kind':'speak','data':{'seat':s,'text':'前置位发言'}} for s in [1,2,3,4]]])
    profile = SeatProfile(seat=5,connection_id=connection.id)
    request = builder.build(profile,connection,view)
    payload = json.loads(request.messages[1].content)
    assert payload['turn_context']['not_spoken_today'] == [5,6,7,8,9]
    assert [line['seat'] for line in payload['turn_context']['heard_public']] == [1,2,3,4]
    assert 7 in payload['decision_facts']['confirmed_good']
    assert any(source['seat']==7 and source['source']=='own_inspection' and source['visibility']=='private'
               for source in payload['decision_facts']['sources'])
    assert '私密查验包装成不存在的公开发言' in request.messages[0].content
    # Yesterday's actual speech remains explicitly dated even when raw history is cropped.
    view['day']=2
    view['events'].append({'id':6,'day':1,'kind':'speak','data':{'seat':7,'text':'昨天的实际发言'}})
    cropped = PromptBuilder(Rulebook.load(),protocol=PLAYER_PROTOCOL,policy=ContextPolicy(soft_char_limit=1))
    payload = json.loads(cropped.build(profile,connection,view).messages[1].content)
    assert not payload['history'] and payload['history_notice']['truncated']
    assert payload['turn_context']['heard_public'][-1] == {'day':1,'seat':7,'kind':'speak','event_id':6}
    assert 7 in payload['turn_context']['not_spoken_today']


@pytest.mark.asyncio
async def test_actual_god_request_uses_larger_allowance_timeout_and_frozen_v10(tmp_path):
    double=ThinkingDouble()
    path=tmp_path/'god.sqlite3'
    app=create_app(path,game_factory=double,reading_scale=0,opening_enabled=False)
    async with opened(app) as client:
        _,headers=await register(client)
        cid=await save(client,headers)
        plan=table(cid)
        report=await admission(client,headers,plan)
        assert all(s['thinking_limits']=={'opening':4096,'first_regular':4096,'first_god':8192,
            'later_regular':8192,'later_god':16384} for s in report['seats'])
        assert report['model_calls']==0 and not double.calls
        created=await client.post('/api/account/games',headers=headers,json=start_body(plan,report))
        runtime=app.state.service.games[created.json()['game']['game_id']]
        assert runtime.engine.snapshot()['wolf_reply_mode']=='sequential'
        await wait_for(lambda: runtime.prepared is not None)
        await client.post(f'/api/games/{runtime.id}/pause',json={'operation_id':'fixture-pause'})
        await wait_for(lambda: runtime.worker is None)
        # A deterministic second-night seer window; the factory is entirely local.
        seer=next(p['seat'] for p in runtime.engine.snapshot()['state']['players'] if p['role']=='seer')
        runtime.prepared=None; runtime.step_id='second-night-buffer'
        runtime.engine._state.update(day=2,phase='seer',queue=[seer])
        before=len(double.calls)
        result=await client.post(f'/api/games/{runtime.id}/resume',json={'operation_id':'second-night'})
        assert result.status_code==200
        await wait_for(lambda: len(double.calls)>before)
        await client.post(f'/api/games/{runtime.id}/pause',json={'operation_id':'done'})
        await wait_for(lambda: runtime.worker is None)
        request=double.calls[before]
        assert request.max_output_tokens==16384 and request.timeout_multiplier==4
        rows=app.state.service.store.attempts(runtime.id)
        record=next(r for r in rows if r['action_kind']=='inspect')
        assert record['timeout_seconds']==120 and record['output_limit']==16384
        assert runtime.snapshot()['player_prompt_version']==PLAYER_PROMPT_VERSION
        gid,token=runtime.id,created.json()['session_token']
        cookies,buffer,count=client.cookies,deepcopy(runtime.prepared),len(double.calls)
    restarted=create_app(path,game_factory=double,reading_scale=0,opening_enabled=False)
    async with opened(restarted) as client:
        client.cookies.update(cookies)
        client.headers['Authorization']=f'Bearer {token}'
        runtime=restarted.state.service.games[gid]
        assert runtime.status=='paused' and runtime.prepared==buffer
        assert runtime.player_prompt_version==PLAYER_PROMPT_VERSION
        assert all(c.generation.thinking_output_tokens==4096 for c in runtime.connections)
        profile=next(p for p in runtime.profiles if p.seat==seer)
        connection=next(c for c in runtime.connections if c.id==profile.connection_id)
        request=PromptBuilder(runtime.book,phase_limits=runtime.phase_limits,protocol=runtime.player_protocol,
            prompt_version=runtime.player_prompt_version,opening_policy=runtime.opening_policy).build(
                profile,connection,runtime.engine.player_view(seer))
        assert request.max_output_tokens==16384 and request.timeout_multiplier==4
        assert (await client.get(f'/api/games/{gid}')).status_code==200
        assert len(double.calls)==count


@pytest.mark.asyncio
async def test_god_recovery_doubles_actual_allowance_once_not_base_times_role_again(tmp_path):
    class OnceTruncated(ThinkingDouble):
        fail=False

        async def complete(self,request):
            if self.fail:
                self.fail=False
                self.calls.append(request)
                raise ProviderError('truncated_response',usage=ModelUsage(10,4096,None,4096))
            return await super().complete(request)

    double=OnceTruncated()
    app=create_app(tmp_path/'recover-god.sqlite3',game_factory=double,reading_scale=0,opening_enabled=False)
    async with opened(app) as client:
        _,headers=await register(client)
        cid=await save(client,headers,cap=1024)
        plan=table(cid)
        created=await client.post('/api/account/games',headers=headers,json=start_body(plan,await admission(client,headers,plan)))
        runtime=app.state.service.games[created.json()['game']['game_id']]
        await wait_for(lambda:runtime.prepared is not None)
        await client.post(f'/api/games/{runtime.id}/pause',json={'operation_id':'fixture'})
        await wait_for(lambda:runtime.worker is None)
        seer=next(p['seat'] for p in runtime.engine.snapshot()['state']['players'] if p['role']=='seer')
        runtime.prepared=None; runtime.step_id='recovery-buffer'
        runtime.engine._state.update(day=2,phase='seer',queue=[seer])
        frozen=[c.decision_config() for c in runtime.connections]
        double.fail=True
        await client.post(f'/api/games/{runtime.id}/resume',json={'operation_id':'truncate'})
        await wait_for(lambda:runtime.status=='paused' and runtime.worker is None)
        assert runtime.decision_recovery.output_limit==4096
        assert runtime.decision_recovery.increased_limit==8192
        before=len(double.calls)
        result=await client.post(f'/api/games/{runtime.id}/resume',json={'operation_id':'recover','recovery':'more_tokens'})
        assert result.status_code==200
        await wait_for(lambda:runtime.prepared is not None)
        assert len(double.calls)==before+1 and double.calls[-1].max_output_tokens==8192
        assert [c.decision_config() for c in runtime.connections]==frozen
        assert runtime.decision_recovery is None
        await client.post(f'/api/games/{runtime.id}/pause',json={'operation_id':'done'})


@pytest.mark.asyncio
async def test_large_request_reaches_transport_with_longer_timeout():
    request,*_=request_for('witch','witch',2)
    timeouts=[]
    def handler(sent):
        timeouts.append(sent.extensions['timeout']['read'])
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{
            'content':'{"action":{"kind":"witch","potion":"skip","target":null},"player_update":{}}'}}]})
    await DeepSeekAdapter('offline-key',timeout=60,transport=httpx.MockTransport(handler)).complete(request)
    assert timeouts==[120]
