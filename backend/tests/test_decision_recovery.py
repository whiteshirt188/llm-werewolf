"""Recovery is an explicit request change, not a hidden retry or model fallback."""

import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest
from test_accounts import register
from test_api import opened
from test_context_and_direct_start import admission, start_body
from test_deepseek_thinking import ThinkingDouble, save, table
from test_gods import configured, night, seat_of
from test_opening import wait_for

from werewolf.agent import ModelResponse, ModelUsage
from werewolf.api import create_app
from werewolf.deepseek import DeepSeekAdapter, ProviderError
from werewolf.demo import simulated_action
from werewolf.engine import Action, GameEngine
from werewolf.player_state import PlayerState
from werewolf.profiles import content_hash


def duel_window():
    game = GameEngine(38, configured("witch", "knight"))
    knight = seat_of(game, "knight")
    night(game, seat_of(game, "villager"), potion="save")
    while game.next_actor() != knight:
        seat = game.next_actor()
        game.submit(seat, simulated_action(game.player_view(seat)))
    return game, knight


@pytest.mark.parametrize("target_role", [None, "wolf", "villager"])
def test_knight_window_is_private_and_only_actual_duel_announces_it(target_role):
    game, knight = duel_window()
    for seat in range(1, 10):
        view = game.player_view(seat)
        assert view["phase"] == ("knight_duel" if seat == knight else "day_speech")
        assert game.player_view(seat, public_only=True)["phase"] == "day_speech"
        assert not any(e["kind"] == "knight_duel" for e in view["events"])
        if seat != knight:
            assert not view["legal_actions"] and "knight" not in view
    game.submit(knight, Action("duel", target=seat_of(game, target_role) if target_role else None))
    public = game.player_view(knight, public_only=True)
    duels = [e for e in public["events"] if e["kind"] == "knight_duel"]
    assert bool(duels) is bool(target_role)
    if target_role:
        assert duels[0]["data"]["seat"] == knight
    else:
        assert public["phase"] == "day_speech" and not game.player_view(knight)["knight"]["used"]


class RecoveringDouble(ThinkingDouble):
    def __init__(self, *, always_fail=False):
        super().__init__()
        self.always_fail = always_fail

    async def complete(self, request):
        if self.always_fail or (not self.calls or request.connection.thinking_enabled and request.max_output_tokens == 4096):
            self.calls.append(request)
            raise ProviderError("truncated_response", retryable=True,
                                usage=ModelUsage(100, request.max_output_tokens, None, request.max_output_tokens))
        response = await super().complete(request)
        data = json.loads(response.content)
        data['player_update'] = {'intent': '恢复后继续记得这条打算'}
        return ModelResponse(json.dumps(data, ensure_ascii=False), response.usage, True)


async def start_paused(app, client, *, cap=4096):
    _, headers = await register(client)
    cid = await save(client, headers, cap=cap)
    plan = table(cid)
    created = await client.post('/api/account/games', headers=headers,
        json=start_body(plan, await admission(client, headers, plan)))
    assert created.status_code == 201, created.text
    runtime = app.state.service.games[created.json()['game']['game_id']]
    await wait_for(lambda: runtime.status == 'paused' and runtime.worker is None)
    return runtime, created.json()['session_token']


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy', ['more_tokens', 'without_thinking'])
async def test_explicit_recovery_once_keeps_frozen_connection_and_buffers_state(tmp_path, strategy):
    double = RecoveringDouble()
    app = create_app(tmp_path / 'recover.sqlite3', game_factory=double, reading_scale=0,
                     game_retry_delay=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, _ = await start_paused(app, client)
        gid = runtime.id
        engine, configs = runtime.engine.snapshot(), [c.decision_config() for c in runtime.connections]
        state = (await client.get(f'/api/games/{gid}')).json()
        assert state['decision_recovery'] == {'output_limit':4096,'output_tokens':4096,
            'reasoning_tokens':4096,'thinking_mode':'requested_low','increased_limit':8192}
        assert all(s not in repr(state['decision_recovery']) for s in ['seat', 'action_kind', 'engine_hash', 'intent'])
        blocked = await client.post(f'/api/games/{gid}/resume', json={'operation_id':'unchanged'})
        assert blocked.status_code == 409 and len(double.calls) == 1
        body = {'operation_id':'changed', 'recovery':strategy}
        result = await client.post(f'/api/games/{gid}/resume', json=body)
        assert result.status_code == 200, result.text
        await wait_for(lambda: runtime.prepared is not None)
        repeat = await client.post(f'/api/games/{gid}/resume', json=body)
        assert repeat.json() == result.json()
        conflict = await client.post(f'/api/games/{gid}/resume', json={**body,'recovery':
            'without_thinking' if strategy == 'more_tokens' else 'more_tokens'})
        assert conflict.status_code == 409
        assert len(double.calls) == 2
        request = double.calls[-1]
        payload = DeepSeekAdapter('offline-key').payload(request)
        assert payload['thinking']['type'] == ('enabled' if strategy == 'more_tokens' else 'disabled')
        assert payload['max_tokens'] == (8192 if strategy == 'more_tokens' else 512)
        assert runtime.engine.snapshot() == engine and not runtime.player_states
        assert [c.decision_config() for c in runtime.connections] == configs
        assert runtime.decision_recovery is None
        rows = app.state.service.store.attempts(gid)
        assert len(rows) == 2 and rows[1]['recovery_strategy'] == strategy
        assert rows[1]['configuration_fingerprint'] != rows[0]['configuration_fingerprint']
        assert rows[1]['timeout_seconds'] == (60 if strategy == 'more_tokens' else 30)
        assert rows[0]['usage']['output_tokens'] == rows[0]['usage']['reasoning_tokens'] == 4096
        assert runtime.prepared['player_update']['intent'] == '恢复后继续记得这条打算'
        assert (await client.post(f'/api/games/{gid}/advance', json={
            'operation_id':'advance', 'expected_step':runtime.step_id})).status_code == 200
        await wait_for(lambda: bool(runtime.player_states))
        assert runtime.player_states[double.calls[0].seat].intent == '恢复后继续记得这条打算'
        await wait_for(lambda: len(double.calls) >= 3)
        assert double.calls[2].connection.thinking_enabled and double.calls[2].max_output_tokens == 4096


@pytest.mark.asyncio
async def test_repeated_exhaustion_has_bounded_user_choices_and_no_automatic_calls(tmp_path):
    double = RecoveringDouble(always_fail=True)
    app = create_app(tmp_path / 'bounded.sqlite3', game_factory=double, reading_scale=0,
                     game_retry_delay=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, _ = await start_paused(app, client)
        for cap in [8192,16384]:
            result = await client.post(f'/api/games/{runtime.id}/resume',
                json={'operation_id':str(cap),'recovery':'more_tokens'})
            assert result.status_code == 200
            await wait_for(lambda: runtime.status == 'paused' and runtime.worker is None)
            assert double.calls[-1].max_output_tokens == cap
        assert runtime.visible()['decision_recovery']['increased_limit'] is None
        assert len(double.calls) == 3
        rejected = await client.post(f'/api/games/{runtime.id}/resume',
            json={'operation_id':'beyond','recovery':'more_tokens'})
        assert rejected.status_code == 409 and len(double.calls) == 3
        result = await client.post(f'/api/games/{runtime.id}/resume',
            json={'operation_id':'off','recovery':'without_thinking'})
        assert result.status_code == 200
        await wait_for(lambda: runtime.status == 'paused' and runtime.worker is None)
        assert len(double.calls) == 4 and not double.calls[-1].connection.thinking_enabled
        assert runtime.engine.snapshot()['state']['revision'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_restart_and_legacy_failure_preserve_the_choice_without_calls(tmp_path, legacy):
    path = tmp_path / 'restart.sqlite3'
    double = RecoveringDouble()
    app = create_app(path, game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, token = await start_paused(app, client)
        snapshot = runtime.snapshot()
        if legacy:
            snapshot.pop('decision_recovery')
            app.state.service.store.commit(runtime.id, snapshot, runtime.visible())
        gid, engine = runtime.id, runtime.engine.snapshot()
    restarted = create_app(path, game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(restarted) as client:
        client.headers['Authorization'] = f'Bearer {token}'
        runtime = restarted.state.service.games[gid]
        assert runtime.status == 'paused' and runtime.decision_recovery
        assert len(double.calls) == 1 and runtime.engine.snapshot() == engine
        assert (await client.post(f'/api/games/{gid}/resume', json={'operation_id':'same'})).status_code == 409
        assert (await client.post(f'/api/games/{gid}/resume', json={
            'operation_id':'off', 'recovery':'without_thinking'})).status_code == 200
        await wait_for(lambda: runtime.prepared is not None)
        assert len(double.calls) == 2


@pytest.mark.asyncio
async def test_legacy_packets_and_receipts_hide_unannounced_knight_for_alive_and_dead_human(tmp_path):
    app = create_app(tmp_path / 'knight.sqlite3', opening_enabled=False)
    async with opened(app) as client:
        response = await client.post('/api/games',json={'seed':38})
        runtime = app.state.service.games[response.json()['game']['game_id']]
        await client.post(f'/api/games/{runtime.id}/pause',json={'operation_id':'fixture-pause'})
        await wait_for(lambda: runtime.worker is None)
        runtime.step_id = None
        runtime.reading_remaining = runtime.reading_deadline = 0
        runtime.engine, knight = duel_window()
        for human in [knight, seat_of(runtime.engine,'wolf'), seat_of(runtime.engine,'villager')]:
            runtime.human_seat = human
            view = runtime.visible()
            assert view['view']['phase'] == 'day_speech'
            if human == knight:
                assert view['view']['legal_actions'][0]['kind'] == 'duel'
            old = deepcopy(view)
            old['view']['phase'] = 'knight_duel'
            assert runtime.filter_legacy_spectator(old)['view']['phase'] == 'day_speech'
            assert old['view']['phase'] == 'knight_duel'
            dead = deepcopy(old)
            dead['view']['alive'] = False
            runtime.engine._player(human)['alive'] = False
            assert runtime.visible()['view']['phase'] == 'day_speech'
            assert runtime.filter_legacy_spectator(dead)['view']['phase'] == 'day_speech'
            runtime.engine._player(human)['alive'] = True


@pytest.mark.asyncio
async def test_stale_recovery_and_configuration_changes_are_rejected_before_calls(tmp_path):
    double = RecoveringDouble()
    app = create_app(tmp_path / 'stale.sqlite3', game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, _ = await start_paused(app, client)
        seat = runtime.decision_recovery.seat
        runtime.player_states[seat] = PlayerState(intent='后来改变的记忆', revision=1)
        result = await client.post(f'/api/games/{runtime.id}/resume',json={
            'operation_id':'stale', 'recovery':'without_thinking'})
        assert result.status_code == 409 and len(double.calls) == 1
        runtime.player_states.clear()
        assert runtime.decision_recovery.engine_hash == content_hash(runtime.engine.snapshot())
        with app.state.accounts.lock:
            app.state.accounts.db.execute('DELETE FROM connections')
        result = await client.post(f'/api/games/{runtime.id}/resume',json={
            'operation_id':'deleted', 'recovery':'without_thinking'})
        assert result.status_code == 200
        await wait_for(lambda: runtime.status == 'paused' and runtime.worker is None)
        assert runtime.pause_reason == 'connection_unavailable' and len(double.calls) == 1


class LateRecoveryDouble(RecoveringDouble):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def complete(self, request):
        if not self.calls:
            return await super().complete(request)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate a transport returning an already-paid response after pause.
            return await super().complete(request)


@pytest.mark.asyncio
async def test_pause_discards_late_recovery_and_keeps_atomic_state(tmp_path):
    double = LateRecoveryDouble()
    app = create_app(tmp_path / 'late.sqlite3', game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, _ = await start_paused(app, client)
        engine, states = runtime.engine.snapshot(), deepcopy(runtime.player_states)
        await client.post(f'/api/games/{runtime.id}/resume',json={
            'operation_id':'off','recovery':'without_thinking'})
        await asyncio.wait_for(double.started.wait(),1)
        await client.post(f'/api/games/{runtime.id}/pause',json={'operation_id':'stop'})
        await wait_for(lambda: runtime.worker is None)
        assert runtime.engine.snapshot() == engine and runtime.player_states == states
        assert runtime.prepared is None and runtime.decision_recovery
        rows = app.state.service.store.attempts(runtime.id)
        assert len(rows) == 2 and rows[-1]['status'] == 'discarded'
        assert rows[-1]['recovery_strategy'] == 'without_thinking' and rows[-1]['usage']['output_tokens'] == 150


@pytest.mark.asyncio
async def test_failed_recovery_commit_does_not_authorize_or_start_a_request(tmp_path, monkeypatch):
    double = RecoveringDouble()
    app = create_app(tmp_path / 'atomic.sqlite3', game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, _ = await start_paused(app, client)
        before = runtime.snapshot()
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError('test disk failure')
        with monkeypatch.context() as patch:
            patch.setattr(app.state.service.store,'commit',unavailable)
            with pytest.raises(sqlite3.OperationalError):
                await app.state.service.control(runtime,'failed','resume','more_tokens')
        assert runtime.snapshot() == before and runtime.worker is None and len(double.calls) == 1
        assert app.state.service.store.receipt(runtime.id,'failed') is None


@pytest.mark.asyncio
async def test_recovered_prefetch_survives_restart_without_another_model_call(tmp_path):
    path = tmp_path / 'prefetch.sqlite3'
    double = RecoveringDouble()
    app = create_app(path, game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(app) as client:
        runtime, token = await start_paused(app, client)
        await client.post(f'/api/games/{runtime.id}/resume',json={
            'operation_id':'off','recovery':'without_thinking'})
        await wait_for(lambda: runtime.prepared is not None)
        gid, buffered, engine = runtime.id, deepcopy(runtime.prepared), runtime.engine.snapshot()
    restarted = create_app(path, game_factory=double, reading_scale=0, opening_enabled=False)
    async with opened(restarted) as client:
        client.headers['Authorization'] = f'Bearer {token}'
        runtime = restarted.state.service.games[gid]
        assert runtime.prepared == buffered and runtime.engine.snapshot() == engine
        assert runtime.decision_recovery is None and len(double.calls) == 2
        assert (await client.post(f'/api/games/{gid}/resume',json={'operation_id':'normal'})).status_code == 200
        await wait_for(lambda: runtime.worker is None)
        assert len(double.calls) == 2
