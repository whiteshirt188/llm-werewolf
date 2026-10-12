"""Opt-in DeepSeek thinking with bounded local transports; never calls a provider."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from test_accounts import register
from test_api import opened
from test_context_and_direct_start import admission, start_body
from test_opening import wait_for
from test_phase_limits import decision

from werewolf.agent import ModelResponse, ModelUsage, PlayerAgent
from werewolf.api import create_app
from werewolf.deepseek import ADAPTER_VERSION, THINKING_ADAPTER_VERSION, DeepSeekAdapter, ProviderError
from werewolf.engine import GameEngine
from werewolf.mock_adapter import MockAdapter
from werewolf.opening import OPENING_PROTOCOL
from werewolf.player_state import PLAYER_PROTOCOL
from werewolf.profiles import GenerationParams, ModelConnection, PersonaProfile, SeatProfile
from werewolf.prompts import PromptBuilder
from werewolf.providers import adapter_version, build_adapter, validate_connection
from werewolf.rulebook import Rulebook


def low_connection(**extra):
    return ModelConnection(id="test", provider="deepseek", model="deepseek-flash",
        base_url="https://api.deepseek.com/v1", adapter_version=THINKING_ADAPTER_VERSION,
        generation=GenerationParams(thinking_mode="low", **extra))


@pytest.mark.parametrize("kind,role", [
    ("propose_kill", "wolf"), ("wolf_message", "wolf"), ("kill", "wolf"), ("inspect", "seer"),
    ("witch", "witch"), ("protect", "guard"), ("speak", "villager"), ("vote", "villager"),
    ("duel", "knight"), ("shoot", "hunter"), ("last_words", "wolf"),
])
def test_low_cap_is_shared_generation_budget_and_game_text_limits_remain(kind, role):
    request, *_ = decision(kind, role, provider="deepseek",
        generation=GenerationParams(thinking_mode="low", thinking_output_tokens=2048,
                                    max_output_tokens=128, target_output_tokens=64))
    payload = DeepSeekAdapter("offline-key").payload(request)
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "low"
    assert payload["max_tokens"] == request.max_output_tokens == 2048
    assert not {"temperature", "thinking_budget", "reasoning_budget"} & payload.keys()
    assert request.reasoning_budget is None
    schema = json.loads(request.response_schema)
    if kind in {"speak", "last_words", "wolf_message", "propose_kill"}:
        assert schema["properties"]["text"]["maxLength"] == (150 if kind in {"wolf_message", "propose_kill"} else 300)


def test_opening_uses_same_opt_in_policy_and_short_intent():
    game = GameEngine(38)
    connection = low_connection(thinking_output_tokens=1024)
    profile = SeatProfile(seat=1, connection_id=connection.id)
    request = PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL).build_opening(profile, connection, game.player_view(1))
    assert request.protocol == OPENING_PROTOCOL and request.max_output_tokens == 1024
    assert json.loads(request.response_schema)["properties"]["intent"]["maxLength"] == 40
    assert build_adapter(connection, "offline-key")._timeout == 60


def test_disabled_legacy_identity_payload_and_timeout_stay_exact():
    old = {"temperature": .7, "max_output_tokens": 768, "target_output_tokens": 256, "reasoning_budget": None}
    connection = ModelConnection(id="test", provider="deepseek", model="old-model",
        base_url="https://api.deepseek.com/v1", generation=old, adapter_version=ADAPTER_VERSION)
    assert connection.decision_config()["generation"] == old
    assert adapter_version("deepseek", "old-model", old) == ADAPTER_VERSION
    assert build_adapter(connection, "offline-key")._timeout == 30
    request, *_ = decision("vote", "villager", provider="deepseek")
    body = DeepSeekAdapter("offline-key").payload(request)
    assert body == {"model": "deepseek-flash", "messages": request.wire_messages(), "stream": False,
        "temperature": .7, "max_tokens": 256, "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    low = low_connection()
    assert adapter_version("deepseek", low.model, low.generation.model_dump()) == THINKING_ADAPTER_VERSION
    assert low.decision_config()["generation"]["thinking_output_tokens"] == 4096


@pytest.mark.parametrize("provider,model,code", [
    ("deepseek", "unverified-alias", "unsupported_thinking_model"),
    ("siliconflow", "zai-org/GLM-5.3", "unsupported_thinking_provider"),
])
def test_unsupported_thinking_stops_before_network(provider, model, code):
    connection = low_connection().model_copy(update={"provider": provider, "model": model,
        "base_url": "https://api.siliconflow.cn/v1" if provider == "siliconflow" else "https://api.deepseek.com/v1"})
    with pytest.raises(ProviderError, match=code):
        validate_connection(connection)


@pytest.mark.parametrize("fields", [{"thinking_mode": "high"}, {"thinking_mode": True},
    {"thinking_output_tokens": 256}, {"thinking_output_tokens": 16385}, {"thinking_output_tokens": 4096.5}])
def test_bad_thinking_configuration_is_rejected(fields):
    with pytest.raises(ValueError):
        GenerationParams(**fields)


@pytest.mark.asyncio
async def test_http_thinking_usage_is_retained_without_disable_warning_or_reasoning_text():
    request, profile, view, builder = decision("speak", "villager", provider="deepseek",
        generation=GenerationParams(thinking_mode="low"))
    sent = []

    def handler(http_request):
        body = json.loads(http_request.content)
        sent.append(body)
        assert body["max_tokens"] == 4096 and body["reasoning_effort"] == "low"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"kind":"speak","text":"2号这句我暂时不认。"}',
            "reasoning_content": "private-thinking-canary"}}], "usage": {"prompt_tokens": 100,
            "completion_tokens": 1500, "completion_tokens_details": {"reasoning_tokens": 1400}}})

    agent = PlayerAgent(profile, request.connection, builder)
    await agent.decide(view, DeepSeekAdapter("offline-key", transport=httpx.MockTransport(handler)))
    record = agent.records[-1]
    assert len(sent) == 1 and record.thinking_mode == "requested_low" and record.output_limit == 4096
    assert record.usage.output_tokens == 1500 and record.usage.reasoning_tokens == 1400
    assert "thinking_disable_not_confirmed" not in record.warnings
    assert "private-thinking-canary" not in repr(record)


class ThinkingDouble:
    simulated = True

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, credential):
        assert credential == "offline-key"
        return self

    async def complete(self, request):
        self.calls.append(request)
        if self.error == "outer_timeout":
            await asyncio.Event().wait()
        if self.error == "late_timeout":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        elif self.error:
            raise ProviderError(self.error, retryable=True, usage=ModelUsage(100, 4096, None, 4000))
        response = await MockAdapter().complete(request)
        if self.error == 'late_timeout':
            data = json.loads(response.content)
            data['player_update'] = {'intent': '这条迟到打算不能保存'}
            response = ModelResponse(json.dumps(data, ensure_ascii=False), simulated=True)
        return ModelResponse(response.content, ModelUsage(100, 150, None, 100 if request.connection.thinking_enabled else 0), True)


async def save(client, headers, mode="low", cid=None, cap=4096):
    response = await client.post('/api/account/connections' + (f'/{cid}' if cid else ''), headers=headers,
        json={"label": mode, "provider": "deepseek", "model": "deepseek-flash", "generation":
              GenerationParams(thinking_mode=mode, thinking_output_tokens=cap).model_dump(), **({"api_key": "offline-key"} if not cid else {})})
    assert response.status_code in {200, 201}, response.text
    return response.json()["id"]


def table(cid, off=None):
    return {"human_seat": 4, "seed": 38, "playback_mode": "manual", "ai_profiles": [
        {"seat": s, "connection_id": off if off and s in {2, 3} else cid,
         "persona": PersonaProfile().model_dump()} for s in [1, 2, 3, 5, 6]]}


@pytest.mark.asyncio
async def test_free_preflight_no_decryption_and_stale_thinking_settings_cannot_start(tmp_path, monkeypatch):
    double = ThinkingDouble()
    app = create_app(tmp_path / 'free.sqlite3', game_factory=double)
    async with opened(app) as client:
        _, headers = await register(client)
        cid = await save(client, headers, "disabled")
        plan = table(cid)
        with monkeypatch.context() as patch:
            patch.setattr(app.state.accounts, 'credential', lambda *args: pytest.fail('free check must not decrypt credentials'))
            before = await admission(client, headers, plan)
        await save(client, headers, cid=cid)
        after = await admission(client, headers, plan)
        assert before['model_calls'] == after['model_calls'] == 0 and not double.calls
        assert all(s['thinking_mode'] == 'low' and s['thinking_output_tokens'] == 4096 for s in after['seats'])
        assert before['seats'][0]['configuration_version'] != after['seats'][0]['configuration_version']
        rejected = await client.post('/api/account/games', headers=headers, json=start_body(plan, before))
        assert rejected.status_code == 409 and not app.state.service.games and not double.calls


@pytest.mark.asyncio
async def test_mixed_opening_action_ledger_and_restart_keep_frozen_modes_and_buffer(tmp_path):
    double = ThinkingDouble()
    path = tmp_path / 'mixed.sqlite3'
    app = create_app(path, game_factory=double, reading_scale=0)
    async with opened(app) as client:
        _, headers = await register(client)
        cid, off = await save(client, headers), await save(client, headers, 'disabled')
        plan = table(cid, off)
        created = await client.post('/api/account/games', headers=headers, json=start_body(plan, await admission(client, headers, plan)))
        assert created.status_code == 201, created.text
        gid = created.json()['game']['game_id']
        runtime = app.state.service.games[gid]
        await wait_for(lambda: not runtime.preparation_active and runtime.prepared is not None)
        assert len([r for r in double.calls if r.protocol == OPENING_PROTOCOL]) == 5
        assert all(r.max_output_tokens == (4096 if r.connection.thinking_enabled else 256)
                   for r in double.calls if r.protocol == OPENING_PROTOCOL)
        assert all(s.intent for s in runtime.player_states.values())
        assert (await client.post(f'/api/games/{gid}/pause', json={'operation_id': 'pause'})).status_code == 200
        await wait_for(lambda: runtime.worker is None or runtime.worker.done())
        buffer, count, states = deepcopy(runtime.prepared), len(double.calls), deepcopy(runtime.player_states)
        rows = app.state.service.store.attempts(gid)
        assert all(a['thinking_mode'] == ('requested_low' if a['seat'] not in {2, 3} else 'requested_disabled') for a in rows)
        assert all('thinking_disable_not_confirmed' not in a['warnings'] for a in rows)
        assert all(a['timeout_seconds'] == (60 if a['seat'] not in {2, 3} else 30) for a in rows)
        cookies, token = client.cookies, created.json()['session_token']
    restarted = create_app(path, game_factory=double, reading_scale=0)
    async with opened(restarted) as client:
        client.cookies.update(cookies)
        client.headers['Authorization'] = f'Bearer {token}'
        runtime = restarted.state.service.games[gid]
        assert runtime.status == 'paused' and runtime.prepared == buffer and runtime.player_states == states
        assert {c.generation.thinking_mode for c in runtime.connections} == {'low', 'disabled'}
        await client.get(f'/api/games/{gid}')
        usage = (await client.get(f'/api/games/{gid}/usage')).json()
        assert usage['known_output_tokens'] == 150 * count
        assert usage['known_reasoning_tokens'] == 100 * len([r for r in double.calls if r.connection.thinking_enabled])
        assert len(double.calls) == count
        assert (await client.post(f'/api/games/{gid}/resume', json={'operation_id': 'resume'})).status_code == 200
        await asyncio.sleep(.01)
        assert len(double.calls) == count  # Prepared action and completed opening are not recalled.


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['timeout', 'truncated_response', 'outer_timeout', 'late_timeout'])
async def test_thinking_timeout_or_truncation_pauses_once_and_late_action_is_discarded(tmp_path, error):
    double = ThinkingDouble(error)
    app = create_app(tmp_path / 'failure.sqlite3', game_factory=double, reading_scale=0,
        game_retry_delay=0, decision_timeout=.01, opening_enabled=False)
    async with opened(app) as client:
        _, headers = await register(client)
        cid = await save(client, headers)
        plan = table(cid)
        created = await client.post('/api/account/games', headers=headers, json=start_body(plan, await admission(client, headers, plan)))
        assert created.status_code == 201, created.text
        gid = created.json()['game']['game_id']
        runtime = app.state.service.games[gid]
        await wait_for(lambda: runtime.status == 'paused')
        rows = app.state.service.store.attempts(gid)
        assert len(double.calls) == len(rows) == 1 and runtime.prepared is None
        assert rows[0]['thinking_mode'] == 'requested_low' and rows[0]['timeout_seconds'] == .02
        assert rows[0]['error'] == ('truncated_response' if error == 'truncated_response' else 'timeout')
        assert 'thinking_disable_not_confirmed' not in rows[0]['warnings']
        assert not runtime.player_states
        if error in {'timeout', 'truncated_response'}:
            assert rows[0]['usage']['output_tokens'] == 4096 and rows[0]['usage']['reasoning_tokens'] == 4000
