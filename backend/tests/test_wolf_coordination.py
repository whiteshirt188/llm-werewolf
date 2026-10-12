"""Sequential private wolf discussion and plan context, all providers simulated."""

import asyncio
import json
from copy import deepcopy

import pytest
from test_api import opened
from test_manual import advance, prepared
from test_opening import wait_for

from werewolf.agent import ModelResponse, ModelUsage
from werewolf.api import create_app
from werewolf.demo import simulated_action
from werewolf.engine import Action, GameConfig, GameEngine, RuleViolation
from werewolf.mock_adapter import MockAdapter
from werewolf.player_state import PLAYER_PROTOCOL, PlayerState
from werewolf.profiles import ModelConnection, SeatProfile, canonical_json, content_hash
from werewolf.prompts import PLAYER_PROMPT_VERSION, TURN_PLAYER_PROMPT_VERSION, ContextPolicy, PromptBuilder
from werewolf.rulebook import Rulebook


def wolves(*, mode='sequential'):
    config=GameConfig(players=9,wolves=3,seers=1,witches=1,knights=1,villagers=3,version='nine-player-v2-gods')
    game=GameEngine(18,config,wolf_reply_mode=mode)
    assert [p['seat'] for p in game.snapshot()['state']['players'] if p['role']=='wolf']==[3,8,9]
    return game


def proposed(*, mode='sequential'):
    game=wolves(mode=mode)
    game.submit(3,Action('propose_kill',target=7,text='留6号，明天我跟他对跳'))
    game.submit(8,Action('propose_kill',target=6,text='我原来想刀6'))
    game.submit(9,Action('propose_kill',target=6,text='我原来想刀6'))
    return game


def request(game,seat,state=None,*,version=PLAYER_PROMPT_VERSION,policy=None):
    c=ModelConnection(id='local',provider='mock',model='offline-policy',base_url='mock://local')
    b=PromptBuilder(Rulebook.load(),protocol=PLAYER_PROTOCOL,player_state=state,
                    prompt_version=version,policy=policy)
    return b.build(SeatProfile(seat=seat,connection_id=c.id),c,game.player_view(seat))


def test_replies_are_immediate_private_and_not_duplicated_or_foreseen():
    game=proposed(); before={s:game.player_view(s) for s in (1,2,4,5,6,7)}
    game.submit(3,Action('wolf_message',text='刀7，留6给我明天对跳'))
    visible=game.player_view(8)
    assert visible['events'][-1]['kind']=='wolf_message'
    assert visible['events'][-1]['data']=={'seat':3,'text':'刀7，留6给我明天对跳'}
    assert not any(e['kind']=='wolf_message' and e['data']['seat']==8 for e in visible['events'])
    assert {s:game.player_view(s) for s in before}==before
    assert not any(e['kind'].startswith('wolf_') for e in game.player_view(8,public_only=True)['events'])
    snap=game.snapshot(); assert snap['wolf_reply_mode']=='sequential'
    restored=GameEngine.from_snapshot(json.loads(canonical_json(snap)))
    assert content_hash(restored.snapshot())==content_hash(snap)
    restored.submit(8,Action('wolf_message',text='同意，留6对跳，我改刀7'))
    with pytest.raises(RuleViolation):restored.submit(8,Action('wolf_message',text='重复发言'))
    assert restored.player_view(9)['events'][-1]['data']['seat']==8
    restored.submit(9,Action('wolf_message',text='看到了8号改刀7，我也配合留6'))
    assert not any(e['kind']=='wolf_replies' for e in restored.player_view(3)['events'])
    assert len([e for e in restored.player_view(3)['events'] if e['kind']=='wolf_message'])==3
    for seat in (3,8,9):restored.submit(seat,Action('kill',target=7))
    assert restored.player_view(3)['events'][-1]['data']['target']==7


def test_batch_snapshot_keeps_old_sealed_replies_and_shape():
    game=proposed(mode='batch'); assert 'wolf_reply_mode' not in game.snapshot()
    restored=GameEngine.from_snapshot(json.loads(canonical_json(game.snapshot())))
    restored.submit(3,Action('wolf_message',text='老局回应'))
    assert not any(e['kind'] in {'wolf_message','wolf_replies'} for e in restored.player_view(8)['events'])
    restored.submit(8,Action('wolf_message',text='老局回应'))
    restored.submit(9,Action('wolf_message',text='老局回应'))
    assert restored.player_view(3)['events'][-1]['kind']=='wolf_replies'
    assert 'wolf_reply_mode' not in restored.snapshot()
    with pytest.raises(RuleViolation):GameEngine(wolf_reply_mode='unknown')


def test_full_teammate_plan_survives_cropping_and_final_vote_remembers_own_intent():
    game=proposed(); game.submit(3,Action('wolf_message',text='刀7，6号留活口给我对跳'))
    payload=json.loads(request(game,8,policy=ContextPolicy(soft_char_limit=1)).messages[1].content)
    assert payload['history']==[]
    team=payload['wolf_coordination']
    assert team['visibility']=='private' and team['proposals'][0]['target']==7
    assert team['proposals'][0]['text']=='留6号，明天我跟他对跳'
    assert team['replies']==[{'seat':3,'text':'刀7，6号留活口给我对跳','event_id':4}]
    assert '提案不是共识' in team['notice']
    game.submit(8,Action('wolf_message',text='我配合刀7，留6让3号对跳'))
    next_request=request(game,9)
    payload=json.loads(next_request.messages[1].content)
    assert [r['seat'] for r in payload['wolf_coordination']['replies']]==[3,8]
    assert any(h['kind']=='wolf_message' and h['visibility']=='private' for h in payload['history'])
    game.submit(9,Action('wolf_message',text='同意改刀7，保留6号'))
    state=PlayerState(intent='刀7，留6给3号明天对跳')
    final=json.loads(request(game,3,state).messages[1].content)
    assert final['player_state']['intent']==state.intent
    assert final['wolf_coordination']['replies'][-1]['seat']==9
    # No fabricated interpretation or obligation to obey another player's proposal.
    assert 6 in final['state']['legal_actions'][0]['targets']
    assert '配合或反对要说清' in next_request.messages[0].content
    assert '别同口径互保暴露同队，也别无意坑队友' in next_request.messages[0].content


def test_v10_retains_old_prompt_and_nonwolves_never_receive_team_context():
    game=proposed(mode='batch')
    old=request(game,3,version=TURN_PLAYER_PROMPT_VERSION)
    assert 'wolf_coordination' not in json.loads(old.messages[1].content)
    assert old.configuration_fingerprint!=request(game,3).configuration_fingerprint
    game.submit(3,Action('wolf_message',text='队内秘密'))
    game.submit(8,Action('wolf_message',text='队内秘密'))
    game.submit(9,Action('wolf_message',text='队内秘密'))
    for s in (3,8,9):game.submit(s,Action('kill',target=7))
    while game.player_view(1)['phase']=='night':
        s=game.next_actor(); view=game.player_view(s)
        game.submit(s,simulated_action(view))
    while game.next_actor()!=1:
        s=game.next_actor();game.submit(s,Action('duel',target=None))
    payload=json.loads(request(game,1).messages[1].content)
    assert 'wolf_coordination' not in payload and '队内秘密' not in canonical_json(payload)


class CoordinatorDouble:
    simulated=True

    def __init__(self):self.calls=[]

    async def complete(self,r):
        self.calls.append(r)
        data=json.loads(r.messages[1].content)
        kind=data['state']['legal_actions'][0]['kind']
        if kind=='wolf_message':
            team=data['wolf_coordination']
            assert team['proposals'][0]['target']==7
            assert '留6号' in team['proposals'][0]['text']
            assert team['replies'][0]['seat']==3
            if r.seat==9:assert team['replies'][-1]['seat']==8
            result={'action':{'kind':kind,'text':'配合3号，刀7留6，公开各自做身份'},
                    'player_update':{'intent':'刀7，留6给3号对跳','reply_to':team['replies'][-1]['event_id']}}
            return ModelResponse(canonical_json(result),ModelUsage(10,10),simulated=True)
        return await MockAdapter().complete(r)


@pytest.mark.asyncio
async def test_service_human_reply_reaches_next_ai_and_prefetch_survives_restart_once(tmp_path):
    path=tmp_path/'team.sqlite3'; double=CoordinatorDouble()
    app=create_app(path,adapter=double,reading_scale=0,opening_enabled=False)
    async with opened(app) as client:
        created=(await client.post('/api/games',json={'seed':18,'human_seat':3,
            'roster':{'players':9,'wolves':3,'seers':1,'witches':1,'knights':1}})).json()
        gid=created['game']['game_id'];runtime=app.state.service.games[gid]
        await client.post(f'/api/games/{gid}/pause',json={'operation_id':'fixture'})
        await wait_for(lambda:runtime.worker is None)
        runtime.engine=proposed();runtime.prepared=None;runtime.step_id=None
        await client.post(f'/api/games/{gid}/resume',json={'operation_id':'begin-chat'})
        state=(await client.get(f'/api/games/{gid}')).json()
        result=await client.post(f'/api/games/{gid}/actions',json={'operation_id':'human-plan',
            'expected_version':state['turn_version'],'action':{'kind':'wolf_message','text':'刀7，留6给我明天对跳'}})
        assert result.status_code==200,result.text
        await prepared(app,gid)
        assert runtime.prepared['seat']==9 and runtime.prepared['player_update']['intent']=='刀7，留6给3号对跳'
        assert runtime.player_states[8].intent=='刀7，留6给3号对跳'
        assert len([e for e in runtime.visible()['view']['events'] if e['kind']=='wolf_message' and e['data']['seat']==8])==1
        assert not any(e['kind']=='wolf_message' and e['data']['seat']==9 for e in runtime.visible()['view']['events'])
        await client.post(f'/api/games/{gid}/pause',json={'operation_id':'pause'})
        await wait_for(lambda:runtime.worker is None)
        buffered,count=deepcopy(runtime.prepared),len(double.calls)
        token=created['session_token']
    restarted=create_app(path,adapter=double,reading_scale=0,opening_enabled=False)
    async with opened(restarted) as client:
        client.headers['Authorization']=f'Bearer {token}'
        runtime=restarted.state.service.games[gid]
        assert runtime.prepared==buffered and runtime.engine.snapshot()['wolf_reply_mode']=='sequential'
        await client.post(f'/api/games/{gid}/resume',json={'operation_id':'resume'})
        await asyncio.sleep(.01)
        assert len(double.calls)==count
        state=(await client.get(f'/api/games/{gid}')).json()
        assert (await advance(client,state,'show-nine')).status_code==200
        await wait_for(lambda:runtime.prepared is None and 9 in runtime.player_states)
        assert len(double.calls)==count
        assert runtime.player_states[9].intent=='刀7，留6给3号对跳'
        for seat in (8,9):
            assert len([e for e in runtime.visible()['view']['events'] if e['kind']=='wolf_message' and e['data']['seat']==seat])==1
        await client.post(f'/api/games/{gid}/pause',json={'operation_id':'done'})
