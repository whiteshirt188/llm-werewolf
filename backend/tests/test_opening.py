"""Production opening flow with local adapters; no provider network or billing."""

import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest
from test_api import opened
from test_qualification import environment
from test_real_games import start_body

from werewolf.agent import ModelResponse, ModelUsage, PlayerAgent
from werewolf.api import create_app
from werewolf.deepseek import ProviderError
from werewolf.demo import simulated_action
from werewolf.engine import GameEngine
from werewolf.mock_adapter import MockAdapter
from werewolf.opening import OPENING_PROTOCOL
from werewolf.player_state import LEGACY_PROTOCOL, PLAYER_PROTOCOL, PlayerState
from werewolf.profiles import (
    GenerationParams,
    ModelConnection,
    PersonaProfile,
    SeatProfile,
    canonical_json,
    content_hash,
)
from werewolf.prompts import (
    FACTION_PLAYER_PROMPT_VERSION,
    PLAYER_PROMPT_VERSION,
    PREVIOUS_PLAYER_PROMPT_VERSION,
    PROMPT_VERSION,
    ROLE_PLAYER_PROMPT_VERSION,
    PromptBuilder,
)
from werewolf.rulebook import Rulebook
from werewolf.service import Runtime
from werewolf.storage import Store


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


class OpeningDouble:
    simulated = True

    def __init__(self, *, held=False, bad=None, late=False):
        self.calls, self.actions, self.releases = [], [], {}
        self.held, self.bad, self.late = held, bad or {}, late
        self.active = self.maximum = 0

    def __call__(self, credential):
        assert credential == "fake-canary-key-never-use-real"
        return self

    async def complete(self, request):
        if request.protocol != OPENING_PROTOCOL:
            self.actions.append(request)
            return await MockAdapter().complete(request)
        self.calls.append(request)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        gate = self.releases.setdefault(request.seat, asyncio.Event())
        try:
            if self.held:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    if not self.late or request.seat != 1:
                        raise
                    await asyncio.sleep(.01)
                    return ModelResponse('{"intent":"取消后的迟到打算"}', ModelUsage(12, 5), simulated=True)
            behavior = self.bad.get(request.seat)
            if behavior == "timeout":
                await asyncio.Event().wait()
            if behavior == "provider":
                raise ProviderError("rate_limit", retryable=True, usage=ModelUsage(7, None))
            content = behavior if behavior else canonical_json({"intent": f"{request.seat}号先观察，再决定怎么打"})
            return ModelResponse(content, ModelUsage(12, 5), simulated=True)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_parallel_preparation_gates_actions_is_private_and_reaches_next_decision(tmp_path):
    double = OpeningDouble(held=True)
    app = create_app(tmp_path / "parallel.sqlite3", adapter=double, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post('/api/games', json={"seed": 38, "human_seat": 4,
            "roster": {"players": 9, "wolves": 3, "hunters": 1}})).json()['game']
        gid = created['game_id']
        runtime = app.state.service.games[gid]
        before = content_hash(runtime.engine.snapshot())
        assert created['preparation'] == {"active": True, "completed": 0, "total": 8}
        assert created['view']['role'] and not created['view']['legal_actions']
        await wait_for(lambda: len(double.calls) == 3)
        await asyncio.sleep(.01)
        assert len(double.calls) == double.maximum == 3
        assert (await client.post(f'/api/games/{gid}/actions', json={"operation_id": "blocked",
            "expected_version": runtime.turn_version, "action": {"kind": "vote", "target": 2}})).status_code == 409
        assert (await client.post(f'/api/games/{gid}/advance', json={"operation_id": "blocked-step",
            "expected_step": created['step_id']})).status_code == 409
        for _ in range(8):
            for gate in double.releases.values():
                gate.set()
            if not runtime.preparation_active:
                break
            await asyncio.sleep(.01)
        await wait_for(lambda: not runtime.preparation_active)
        await wait_for(lambda: runtime.prepared is not None or runtime.engine.next_actor() == 4)
        assert len(double.calls) == 8 and double.maximum == 3
        assert content_hash(runtime.engine.snapshot()) == before
        for request in double.calls:
            payload = json.loads(request.messages[-1].content)
            view = runtime.engine.player_view(request.seat)
            assert payload['state']['role'] == view['role']
            assert payload['state']['wolf_teammates'] == view['wolf_teammates']
            assert all(set(p) == {'seat', 'alive'} for p in payload['state']['players'])
            assert not {'legal_actions', 'checks', 'witch', 'events'} & payload['state'].keys()
            assert request.max_output_tokens == 256
            assert runtime.player_states[request.seat].intent.startswith(f'{request.seat}号')
        for request in double.actions:
            payload = json.loads(request.messages[-1].content)
            assert payload['player_state']['intent'] == runtime.player_states[request.seat].intent
        visible = (await client.get(f'/api/games/{gid}')).json()
        assert visible['preparation'] == {"active": False, "completed": 8, "total": 8}
        packets = (await client.get(f'/api/games/{gid}/events')).json()
        usage = (await client.get(f'/api/games/{gid}/usage')).json()
        assert usage['calls'] == len(double.calls) + len(double.actions)
        for intent in (s.intent for s in runtime.player_states.values()):
            assert intent not in canonical_json(visible) + canonical_json(packets) + canonical_json(usage)
        assert all(a['action'] is None for a in app.state.service.store.attempts(gid)
                   if a['purpose'] == 'opening_preparation')
        finished = GameEngine.from_snapshot(runtime.engine.snapshot())
        for _ in range(250):
            if finished.finished:
                break
            seat = finished.next_actor()
            finished.submit(seat, simulated_action(finished.player_view(seat)))
        assert finished.finished
        async with runtime.lock:
            app.state.service._commit(runtime, finished, 'finished')
        replay = (await client.get(f'/api/games/{gid}/replay')).json()
        assert all('player_update' not in a for a in replay['usage'])
        assert all(s.intent not in canonical_json(replay) for s in runtime.player_states.values())


@pytest.mark.asyncio
async def test_real_failure_and_timeout_skip_once_and_preserve_billed_usage(tmp_path):
    double = OpeningDouble(bad={1: '{"intent":"' + '长' * 41 + '"}', 2: 'timeout', 3: 'provider'})
    async with environment(tmp_path, qualification_enabled=False, opening_enabled=True,
                           game_factory=double, decision_timeout=.03, reading_scale=0) as (app, client, _, headers, cid, exam):
        body = await start_body(client, headers, cid, human=4)
        assert not double.calls and not exam.calls
        response = await client.post('/api/account/games', headers=headers, json=body)
        assert response.status_code == 201, response.text
        gid = response.json()['game']['game_id']
        runtime = app.state.service.games[gid]
        await wait_for(lambda: not runtime.preparation_active)
        assert len(double.calls) == 5 and len({r.seat for r in double.calls}) == 5
        assert runtime.opening_seats[1] == runtime.opening_seats[2] == runtime.opening_seats[3] == 'skipped'
        assert runtime.opening_seats[5] == runtime.opening_seats[6] == 'done'
        assert runtime.status == 'running'
        records = [a for a in app.state.service.store.attempts(gid) if a['purpose'] == 'opening_preparation']
        assert len(records) == 5 and all(a['retry'] == 0 for a in records)
        assert next(a for a in records if a['seat'] == 1)['usage']['input_tokens'] == 12
        assert next(a for a in records if a['seat'] == 3)['usage']['input_tokens'] == 7
        assert next(a for a in records if a['seat'] == 2)['usage']['input_tokens'] is None


@pytest.mark.asyncio
async def test_pause_keeps_success_discards_late_response_and_resume_only_unsent(tmp_path):
    double = OpeningDouble(held=True, late=True)
    path = tmp_path / 'pause.sqlite3'
    app = create_app(path, adapter=double, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post('/api/games', json={"seed": 38, "human_seat": 4})).json()
        gid, token = created['game']['game_id'], created['session_token']
        runtime = app.state.service.games[gid]
        await wait_for(lambda: len(double.calls) == 3)
        double.releases[2].set()
        await wait_for(lambda: len(double.calls) == 4)
        assert runtime.opening_seats[2] == 'done'
        await client.post(f'/api/games/{gid}/pause', json={"operation_id": "pause"})
        await wait_for(lambda: runtime.worker is None)
        assert runtime.opening_seats[1] == 'skipped' and 1 not in runtime.player_states
        assert runtime.player_states[2].intent.startswith('2号')
        saved_calls = {r.seat for r in double.calls}
    resumed_double = OpeningDouble()
    restarted = create_app(path, adapter=resumed_double, reading_scale=0)
    async with opened(restarted) as client:
        client.headers['Authorization'] = f'Bearer {token}'
        runtime = restarted.state.service.games[gid]
        assert runtime.player_states[2].intent.startswith('2号')
        response = await client.post(f'/api/games/{gid}/resume', json={"operation_id": "resume"})
        assert response.status_code == 200
        await wait_for(lambda: not runtime.preparation_active)
        assert len(resumed_double.calls) == 1
        assert all(r.seat not in saved_calls for r in resumed_double.calls)


@pytest.mark.asyncio
async def test_crash_restore_skips_inflight_and_does_not_call_until_resume(tmp_path):
    source, copy = tmp_path / 'source.sqlite3', tmp_path / 'crash.sqlite3'
    double = OpeningDouble(held=True)
    app = create_app(source, adapter=double, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post('/api/games', json={"seed": 38})).json()
        gid, token = created['game']['game_id'], created['session_token']
        await wait_for(lambda: len(double.calls) == 3)
        with sqlite3.connect(copy) as target:
            app.state.service.store.db.backup(target)
    restored_double = OpeningDouble()
    restarted = create_app(copy, adapter=restored_double, reading_scale=0)
    async with opened(restarted) as client:
        client.headers['Authorization'] = f'Bearer {token}'
        runtime = restarted.state.service.games[gid]
        assert runtime.status == 'paused' and not restored_double.calls
        assert list(runtime.opening_seats.values()).count('skipped') == 3
        assert all(a['status'] == 'interrupted' for a in restarted.state.service.store.attempts(gid))
        await client.post(f'/api/games/{gid}/resume', json={"operation_id": "resume"})
        await wait_for(lambda: not runtime.preparation_active)
        assert len(restored_double.calls) == 2
        assert {r.seat for r in restored_double.calls}.isdisjoint({r.seat for r in double.calls})


@pytest.mark.asyncio
async def test_opening_intent_and_attempt_commit_roll_back_together(tmp_path):
    app = create_app(tmp_path / 'atomic.sqlite3', reading_scale=0)
    async with opened(app) as client:
        gid = (await client.post('/api/games', json={"seed": 38})).json()['game']['game_id']
        runtime = app.state.service.games[gid]
        await wait_for(lambda: not runtime.preparation_active)
        before = deepcopy(runtime.snapshot())
        with pytest.raises(ValueError):
            app.state.service._commit(runtime, opening_update=(2, 'done'), opening_intent='不该存入的打算',
                                      attempt=('missing-attempt', {"status": "ok"}))
        assert runtime.snapshot() == before
        assert content_hash(app.state.service.store.load_games()[0]) == content_hash(before)


@pytest.mark.parametrize('role', ['wolf', 'seer', 'witch', 'villager', 'hunter', 'guard', 'knight'])
def test_three_documents_render_role_and_real_win_condition(role):
    book = Rulebook.load()
    for condition in ['eliminate_side', 'parity']:
        selected = book.select(role, 'opening_plan', {'win_condition': condition})
        assert [n for n, _ in selected] == ['游戏.md', f'roles/werewolf-{role}/身份.md', '规则.md']
        assert not any('{本局胜负}' in t or '{胜负规则}' in t for _, t in selected)
        assert ('村民全灭或神职全灭' if condition == 'eliminate_side' else '存活狼人不少于好人') in selected[1][1]
    assert len(dict(book.files)['游戏.md']) <= 150
    assert len(dict(book.select(role, 'opening_plan', {'win_condition': 'eliminate_side'}))['规则.md']) <= 1000
    assert len(book.select(role, 'speak', {'win_condition': 'eliminate_side'})[1][1]) <= 200


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['{"intent":""}', '{"intent":null}', '{"intent":"a","action":{}}',
                                   '{"intent":"a","intent":"b"}', '{"intent":"' + '长' * 41 + '"}'])
async def test_invalid_opening_output_has_no_action_or_update(content):
    from werewolf.agent import InvalidModelResponse
    game = GameEngine(38)
    connection = ModelConnection(id='m', provider='mock', model='fixed', base_url='mock://local')
    agent = PlayerAgent(SeatProfile(seat=2, connection_id='m'), connection,
                        PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL))
    before = game.snapshot()
    double = OpeningDouble(bad={2: content})
    with pytest.raises(InvalidModelResponse):
        await agent.prepare_opening(game.player_view(2), double)
    assert game.snapshot() == before and agent.records[-1].player_update is None


def test_v8_keeps_original_prompts_while_v9_removes_repeated_guidance():
    from test_role_skills import view_for
    view = view_for()
    c = ModelConnection(id='m', provider='mock', model='fixed', base_url='mock://local')
    p = SeatProfile(seat=view['seat'], connection_id='m')
    old = PromptBuilder(Rulebook.load(legacy=True), protocol=PLAYER_PROTOCOL, prompt_version=ROLE_PLAYER_PROMPT_VERSION)
    new = PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL, player_state=PlayerState(intent='先观察'))
    before, after = old.build(p, c, view), new.build(p, c, view)
    assert '【角色行动底线】' in before.messages[0].content
    assert '【持续玩家状态与互动】' in before.messages[0].content
    assert '【角色行动底线】' not in after.messages[0].content
    assert '【持续玩家状态与互动】' not in after.messages[0].content
    assert json.loads(after.messages[1].content)['player_state']['intent'] == '先观察'
    assert before.response_schema == after.response_schema
    assert after.configuration_fingerprint != before.configuration_fingerprint
    assert new._prompt_version == PLAYER_PROMPT_VERSION


@pytest.mark.asyncio
async def test_timeout_response_that_ignores_cancellation_is_skipped_with_usage(tmp_path):
    double = OpeningDouble(held=True, late=True)
    app = create_app(tmp_path / 'late-timeout.sqlite3', adapter=double, decision_timeout=.02, reading_scale=0)
    async with opened(app) as client:
        gid = (await client.post('/api/games', json={"seed": 38, "human_seat": 4})).json()['game']['game_id']
        runtime = app.state.service.games[gid]
        await wait_for(lambda: not runtime.preparation_active)
        assert runtime.opening_seats[1] == 'skipped' and 1 not in runtime.player_states
        record = next(a for a in app.state.service.store.attempts(gid) if a['seat'] == 1)
        assert record['error'] == 'timeout' and record['usage']['output_tokens'] == 5
        assert len(double.calls) == 5


@pytest.mark.parametrize('cap,target,expected', [(768, 512, 256), (96, 256, 96), (768, 64, 64)])
def test_opening_respects_user_limits_and_existing_non_thinking_payload(cap, target, expected):
    from werewolf.deepseek import DeepSeekAdapter
    from werewolf.siliconflow import SiliconFlowAdapter
    for provider, model, url, adapter in [
        ('deepseek', 'deepseek-chat', 'https://api.deepseek.com/v1', DeepSeekAdapter),
        ('siliconflow', 'tencent/Hy4-preview', 'https://api.siliconflow.cn/v1', SiliconFlowAdapter),
    ]:
        connection = ModelConnection(id='m', provider=provider, model=model, base_url=url,
            generation=GenerationParams(max_output_tokens=cap, target_output_tokens=target))
        request = PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL).build_opening(
            SeatProfile(seat=2, connection_id='m'), connection, GameEngine(38).player_view(2))
        payload = adapter('fake-canary-key-never-use-real').payload(request)
        assert payload['max_tokens'] == request.max_output_tokens == expected
        assert payload['model'] == model and payload['response_format'] == {'type': 'json_object'}
        if provider == 'deepseek':
            assert payload['thinking'] == {'type': 'disabled'}
        else:
            assert payload['enable_thinking'] is False and payload['thinking_budget'] == 512


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [PROMPT_VERSION, PREVIOUS_PLAYER_PROMPT_VERSION,
                                   FACTION_PLAYER_PROMPT_VERSION, ROLE_PLAYER_PROMPT_VERSION])
async def test_old_game_restores_exact_frozen_request_and_never_prepares(tmp_path, version):
    from hashlib import sha256
    from uuid import uuid4
    path = tmp_path / f'{version}.sqlite3'
    game = GameEngine(38)
    connection = ModelConnection(id='m', provider='mock', model='fixed', base_url='mock://local')
    profiles = [SeatProfile(seat=s, connection_id='m', persona=PersonaProfile(play_style='playful'))
                for s in range(1, 7) if s != 4]
    protocol = LEGACY_PROTOCOL if version == PROMPT_VERSION else PLAYER_PROTOCOL
    runtime = Runtime(id=uuid4().hex, engine=game, human_seat=4, token_hash=sha256(b'old-token').hexdigest(),
                      book=Rulebook.load(legacy=True), connections=[connection], profiles=profiles,
                      player_protocol=protocol, player_prompt_version=version, step_id='old-step', step_duration=0)
    builder = PromptBuilder(runtime.book, protocol=protocol, prompt_version=version)
    before = builder.build(profiles[0], connection, game.player_view(1))
    store = Store(path)
    store.commit(runtime.id, runtime.snapshot(), runtime.visible())
    store.close()
    double = OpeningDouble()
    app = create_app(path, adapter=double, reading_scale=0)
    async with opened(app) as client:
        client.headers['Authorization'] = 'Bearer old-token'
        restored = app.state.service.games[runtime.id]
        assert restored.status == 'paused' and restored.opening_policy is None
        assert restored.book.files == runtime.book.files
        await client.post(f'/api/games/{runtime.id}/resume', json={"operation_id": 'restore'})
        await wait_for(lambda: restored.prepared is not None)
        assert len(double.actions) == 1 and not double.calls
        after = double.actions[0]
        assert after.wire_messages() == before.wire_messages()
        assert after.configuration_fingerprint == before.configuration_fingerprint
        assert 'opening_policy' not in restored.snapshot()
