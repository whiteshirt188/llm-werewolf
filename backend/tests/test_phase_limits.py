"""Stage request caps and durable vendor evidence; local transports/fake keys only."""

import asyncio
import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict

import httpx
import pytest
from app_helpers import flow_app as create_app
from test_api import opened
from test_context_and_direct_start import admission, start_body
from test_deepseek import envelope
from test_manual import prepared
from test_qualification import environment
from test_real_games import GameDouble
from test_table_plan import plan

from werewolf import GameEngine
from werewolf.agent import ModelUsage, PlayerAgent
from werewolf.deepseek import DeepSeekAdapter, ProviderError
from werewolf.profiles import GenerationParams, ModelConnection, SeatProfile
from werewolf.prompts import DEFAULT_PHASE_LIMITS, PhaseLimits, PromptBuilder
from werewolf.rulebook import Rulebook
from werewolf.siliconflow import GLM_MODEL, OFFICIAL_URL, SiliconFlowAdapter


def decision(kind="speak", role="wolf", *, provider="siliconflow", generation=None, phase_limits=DEFAULT_PHASE_LIMITS):
    c = ModelConnection(id="chosen", provider=provider, model=GLM_MODEL if provider == "siliconflow" else "deepseek-flash",
                        base_url=OFFICIAL_URL if provider == "siliconflow" else "https://api.deepseek.com/v1",
                        generation=generation or GenerationParams())
    view = GameEngine(38).player_view(1)
    view.update(role=role, phase="hunter_shot" if kind == "shoot" else "last_words" if kind == "last_words" else "day")
    spec = {"kind": kind}
    if kind in {"speak", "last_words", "wolf_message", "propose_kill"}:
        spec.update(max_length=150 if kind in {"wolf_message", "propose_kill"} else 300)
    if kind in {"propose_kill", "kill", "inspect", "protect", "shoot", "duel", "vote"}:
        spec.update(targets=[2], allow_abstain=kind in {"protect", "shoot", "duel", "vote"})
    if kind == "witch":
        spec.update(potions={"skip": [], "save": [2], "poison": [2]})
    view["legal_actions"] = [spec]
    builder = PromptBuilder(Rulebook.load(), phase_limits=phase_limits)
    profile = SeatProfile(seat=1, connection_id=c.id)
    return builder.build(profile, c, view), profile, view, builder


@pytest.mark.parametrize("kind,role,phase,budget,output", [
    ("propose_kill", "wolf", "night", 512, 512),
    ("wolf_message", "wolf", "night", 512, 512),
    ("kill", "wolf", "night", 512, 256),
    ("inspect", "seer", "night", 512, 256),
    ("witch", "witch", "night", 512, 256),
    ("protect", "guard", "night", 512, 256),
    ("speak", "villager", "day", 1024, 768),
    ("vote", "villager", "day", 1024, 256),
    ("duel", "knight", "day", 1024, 256),
    ("shoot", "hunter", "day", 1024, 256),
    ("last_words", "wolf", "day", 1024, 768),
])
def test_action_routing_not_an_ambiguous_calendar_day(kind, role, phase, budget, output):
    request, _, _, _ = decision(kind, role)
    payload = SiliconFlowAdapter("fake").payload(request)
    assert request.reasoning_phase == phase and request.reasoning_budget == budget
    assert payload["thinking_budget"] == budget and payload["enable_thinking"] is False
    assert payload["max_tokens"] == output
    assert payload["model"] == GLM_MODEL and "reasoning_effort" not in payload
    assert len(payload["messages"]) == 2 and "游戏.md" in payload["messages"][0]["content"]


def test_saved_lower_caps_are_not_raised_and_legacy_requests_are_unchanged():
    generation = GenerationParams(max_output_tokens=128, target_output_tokens=64)
    for kind, expected in [("speak",128), ("propose_kill",128), ("kill",64)]:
        request, *_ = decision(kind, generation=generation)
        assert request.max_output_tokens == expected
    legacy, *_ = decision("propose_kill", phase_limits=None)
    current, *_ = decision("propose_kill")
    assert legacy.max_output_tokens == 768 and legacy.reasoning_budget is None
    assert "thinking_budget" not in SiliconFlowAdapter("fake").payload(legacy)
    assert legacy.configuration_fingerprint != current.configuration_fingerprint
    changed, *_ = decision("propose_kill", phase_limits=PhaseLimits(night_reasoning=256))
    assert changed.configuration_fingerprint != current.configuration_fingerprint


@pytest.mark.parametrize("kind", ["speak", "kill"])
def test_deepseek_stays_disabled_without_fictitious_budget_or_effort(kind):
    request, *_ = decision(kind, provider="deepseek")
    payload = DeepSeekAdapter("fake").payload(request)
    assert payload["thinking"] == {"type":"disabled"}
    assert request.reasoning_budget is None
    assert "reasoning_effort" not in payload and "thinking_budget" not in payload


@pytest.mark.asyncio
async def test_rejected_budget_never_falls_back_to_enabled_or_uncapped_request():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400)

    request, *_ = decision("kill")
    with pytest.raises(ProviderError, match="request_parameters"):
        await SiliconFlowAdapter("fake", transport=httpx.MockTransport(handler)).complete(request)
    assert len(calls) == 1 and calls[0]["thinking_budget"] == 512 and calls[0]["enable_thinking"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reported,warning", [(None,False), (0,False), (512,False), (513,True)])
async def test_actual_usage_not_claimed_as_free_or_rewritten_to_cap(reported, warning):
    _, profile, view, builder = decision("speak")
    request, *_ = decision("propose_kill")
    # Public speech has a 1024 budget; use a 512 policy here to probe the exact boundary.
    builder = PromptBuilder(Rulebook.load(), phase_limits=PhaseLimits(day_reasoning=512))
    adapter = SiliconFlowAdapter("fake", simulated=True, transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json=envelope('{"kind":"speak","text":"我先听发言。"}', usage={
            "prompt_tokens":100, "completion_tokens":2000,
            "completion_tokens_details":{"reasoning_tokens":reported}}))))
    agent = PlayerAgent(profile, request.connection, builder)
    await agent.decide(view, adapter)
    record = agent.records[-1]
    assert record.usage.reasoning_tokens == reported and record.usage.output_tokens == 2000
    assert ("reasoning_budget_exceeded" in record.warnings) is warning
    assert ("thinking_disable_not_confirmed" in record.warnings) is (reported is not None and reported > 0)
    assert record.reasoning_budget == 512 and record.thinking_mode == "requested_disabled"


@pytest.mark.asyncio
async def test_caps_are_frozen_across_commits_and_restart_and_reads_do_not_call(tmp_path):
    game = GameDouble()
    async with environment(tmp_path, qualification_enabled=False, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, _):
        table = plan(cid)
        table["human_seat"] = 4
        table["ai_profiles"] = [{**p, "seat":s} for p,s in zip(table["ai_profiles"], [1,2,3,5,6], strict=True)]
        response = await client.post("/api/account/games", headers=headers,
                                     json=start_body(table, await admission(client, headers, table)))
        created = response.json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        runtime = app.state.service.games[gid]
        assert runtime.phase_limits == PhaseLimits()
        app.state.service._commit(runtime, status="paused", pause_reason="test")
        snapshot = runtime.snapshot()
        assert snapshot["phase_limits"] == asdict(PhaseLimits())
        buffer = deepcopy(runtime.prepared)
        assert game.calls[0].reasoning_phase == "night" and game.calls[0].max_output_tokens == 512
    restarted = create_app(tmp_path / "game.sqlite3", game_factory=game)
    async with opened(restarted) as client:
        client.headers["Authorization"] = f"Bearer {created['session_token']}"
        runtime = restarted.state.service.games[gid]
        assert runtime.phase_limits == PhaseLimits() and runtime.prepared == buffer
        assert runtime.status == "paused"
        await client.get(f"/api/games/{gid}")
        await client.get(f"/api/games/{gid}/usage")
        assert len(game.calls) == 1


@pytest.mark.asyncio
async def test_reported_error_usage_and_retry_each_preserve_phase_request_limits(tmp_path):
    def fail(_):
        raise ProviderError("truncated_response", retryable=True, usage=ModelUsage(100,2000,None,1300))

    game = GameDouble(fail=fail)
    async with environment(tmp_path, qualification_enabled=False, game_factory=game, reading_scale=0) as (app, client, _, headers, _, _):
        app.state.service.retry_delay = 0
        c = (await client.post("/api/account/connections", headers=headers, json={
            "label":"fake", "provider":"siliconflow", "model":GLM_MODEL,
            "api_key":"fake-canary-key-never-use-real"})).json()
        table = plan(c["id"])
        table["human_seat"] = 4
        table["ai_profiles"] = [{**p, "seat":s} for p,s in zip(table["ai_profiles"], [1,2,3,5,6], strict=True)]
        response = await client.post("/api/account/games", headers=headers,
                                     json=start_body(table, await admission(client, headers, table)))
        gid = response.json()["game"]["game_id"]
        async with asyncio.timeout(2):
            while app.state.service.games[gid].status != "paused":
                await asyncio.sleep(.001)
        attempts = app.state.service.store.attempts(gid)
        assert len(attempts) == len(game.calls) == 3
        assert all(r.reasoning_budget == 512 and r.connection.model == GLM_MODEL for r in game.calls)
        assert all(a["reasoning_budget"] == 512 and "reasoning_budget_exceeded" in a["warnings"]
                   and a["usage"]["reasoning_tokens"] == 1300 for a in attempts)
        assert app.state.service.games[gid].status == "paused"


@pytest.mark.asyncio
async def test_legacy_saved_game_missing_policy_is_not_retroactively_changed(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    app = create_app(path, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed":38,"human_seat":4})).json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        await client.post(f"/api/games/{gid}/pause", json={"operation_id":"pause"})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE games SET snapshot=json_remove(snapshot,'$.phase_limits') WHERE id=?", (gid,))
    restarted = create_app(path, reading_scale=0)
    async with opened(restarted):
        runtime = restarted.state.service.games[gid]
        assert runtime.phase_limits is None and runtime.status == "paused"
        profile = runtime.profiles[0]
        connection = next(c for c in runtime.connections if c.id == profile.connection_id)
        request = PromptBuilder(runtime.book, phase_limits=runtime.phase_limits).build(
            profile, connection, runtime.engine.player_view(profile.seat))
        assert request.max_output_tokens == 768 and request.reasoning_budget is None
        assert len(restarted.state.service.store.attempts(gid)) == 1


@pytest.mark.parametrize("settings", [{"night_reasoning":127}, {"day_reasoning":128}, {"night_output":0}, {"day_reasoning":True}])
def test_invalid_or_inverted_phase_limits_fail_locally(settings):
    with pytest.raises(ValueError):
        PhaseLimits(**settings)
