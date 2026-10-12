"""Real wiring tests with fake credentials, scripted exams and local-only model doubles."""

import asyncio
import json
from copy import deepcopy

import pytest
from app_helpers import flow_app as create_app
from test_api import opened, ready, submit_body
from test_manual import advance, prepared
from test_qualification import environment, finish, start
from test_table_plan import plan

from werewolf.agent import ModelResponse, ModelUsage
from werewolf.deepseek import ProviderError
from werewolf.mock_adapter import MockAdapter
from werewolf.profiles import PersonaProfile, content_hash


class GameDouble:
    simulated = True

    def __init__(self, fail=None, hold=False):
        self.calls, self.fail = [], fail
        self.hold = asyncio.Event() if hold else None
        self.started = asyncio.Event()

    def __call__(self, credential):
        assert credential == "fake-canary-key-never-use-real"
        return self

    async def complete(self, request):
        self.calls.append(request)
        self.started.set()
        if self.hold:
            await self.hold.wait()
        if self.fail:
            self.fail(len(self.calls))
        response = await MockAdapter().complete(request)
        return ModelResponse(response.content, ModelUsage(100, 10, 50, None), simulated=True)


async def qualified(app, client, headers, connection_id):
    run, _ = await start(client, headers, connection_id)
    await finish(app, client, run)


async def start_body(client, headers, connection_id, *, human=4, mode="manual", operation="real-start"):
    table = plan(connection_id)
    table["human_seat"] = human
    table["playback_mode"] = mode
    table["ai_profiles"] = [{"seat": s, "nickname": f"AI {s}", "connection_id": connection_id,
                             "persona": PersonaProfile().model_dump()} for s in range(1, 7) if s != human]
    admission = (await client.post("/api/account/table-plan/admission", headers=headers, json=table)).json()
    return {"plan": table, "operation_id": operation, "confirm_cost": True,
            "expected_versions": {str(s["seat"]): s["configuration_version"] for s in admission["seats"]}}


@pytest.mark.asyncio
async def test_admission_cost_csrf_staleness_atomic_start_replay_and_current_recovery(tmp_path):
    game = GameDouble()
    async with environment(tmp_path, game_factory=game, reading_scale=0) as (app, client, user, headers, cid, _):
        body = await start_body(client, headers, cid)
        assert (await client.post("/api/account/games", headers=headers, json=body)).status_code == 403
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid)
        assert (await client.post("/api/account/games", json=body)).status_code == 403
        assert (await client.post("/api/account/games", headers=headers, json={**body, "confirm_cost":False})).status_code == 422
        assert (await client.post("/api/account/games", headers=headers, json={**body, "expected_versions":{}})).status_code == 409
        response = await client.post("/api/account/games", headers=headers, json=body)
        assert response.status_code == 201, response.text
        created = response.json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        assert len(game.calls) == 1
        for _ in range(3):
            repeated = await client.post("/api/account/games", headers=headers, json=body)
            assert repeated.json()["session_token"] == created["session_token"]
            assert repeated.json()["game"]["game_id"] == gid
            assert (await client.get("/api/account/games/current")).json()["session_token"] == created["session_token"]
        conflicting = deepcopy(body)
        conflicting["plan"]["seed"] = 37
        assert (await client.post("/api/account/games", headers=headers, json=conflicting)).status_code == 409
        assert (await client.post("/api/account/games", headers=headers, json={**body, "operation_id":"new"})).status_code == 409
        assert len(game.calls) == 1
        receipt = app.state.service.store.game_start(user, "real-start")
        assert receipt and created["session_token"] not in repr(receipt)
        assert "real_binding" not in json.dumps(created)
        snapshot = app.state.service.games[gid].snapshot()
        assert "fake-canary-key" not in json.dumps(snapshot) and created["session_token"] not in json.dumps(snapshot)
        assert app.state.service.store.db.execute("SELECT count(*) FROM game_starts").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("human", [1, 3, 4, 5])
async def test_complete_real_wiring_three_human_roles_fixed_model_private_views_and_usage(tmp_path, human):
    game = GameDouble()
    async with environment(tmp_path, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, exam):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid, human=human, mode="auto")
        result = await client.post("/api/account/games", headers=headers, json=body)
        assert result.status_code == 201, result.text
        gid = result.json()["game"]["game_id"]
        for _ in range(100):
            state = await ready(client, gid)
            if state["status"] == "finished":
                break
            assert state["status"] == "running"
            assert (await client.post(f"/api/games/{gid}/actions", json=submit_body(state))).status_code == 200
        else:
            pytest.fail("real wiring game did not finish")
        replay = (await client.get(f"/api/games/{gid}/replay")).json()
        assert replay["mode"] == "real" and replay["winner"] in {"good", "wolves"}
        count = len(game.calls)
        for _ in range(3):
            await client.get(f"/api/games/{gid}")
            await client.get(f"/api/games/{gid}/stream?follow=false")
            await client.get(f"/api/games/{gid}/replay")
        summary = (await client.get(f"/api/games/{gid}/usage")).json()
        assert count == len(game.calls) == summary["calls"] == len(replay["usage"])
        assert summary["known_input_tokens"] == 100 * count and summary["known_output_tokens"] == 10 * count
        assert summary["known_cached_input_tokens"] == 50 * count
        assert summary["unknown_reasoning_usage_calls"] == count
        assert "seat" not in json.dumps(summary) and "action" not in json.dumps(summary)
        assert len(exam.calls) == 12  # Game and exam ledgers remain separate.
        roles = {p["seat"]: p["role"] for p in replay["players"]}
        for call in game.calls:
            view = json.loads(call.messages[-1].content)["state"]
            assert call.connection.model == "fixed-chosen-model" and call.connection.id == cid
            assert view["seat"] == call.seat and call.seat != human
            assert view["role"] == roles[call.seat]
            assert "fake-canary-key" not in repr(call) and "revealed_roles" not in view
            if view["role"] != "wolf":
                assert not view["wolf_teammates"]
            if view["role"] != "seer":
                assert not view["checks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,calls", [("authentication",1), ("balance",1), ("timeout",3), ("rate_limit",3)])
async def test_failure_retry_limit_unknown_usage_no_settlement_and_no_fallback(tmp_path, code, calls):
    def fail(_):
        raise ProviderError(code, retryable=code not in {"authentication","balance"})
    game = GameDouble(fail)
    async with environment(tmp_path, game_factory=game, game_retry_delay=0, reading_scale=0) as (app, client, _, headers, cid, _):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid)
        result = (await client.post("/api/account/games", headers=headers, json=body)).json()
        gid = result["game"]["game_id"]
        before = app.state.service.games[gid].engine.snapshot()
        state = await ready(client, gid)
        assert state["status"] == "paused" and state["pause_reason"] == code
        assert app.state.service.games[gid].engine.snapshot() == before
        ledger = app.state.service.store.attempts(gid)
        assert len(game.calls) == len(ledger) == calls
        assert [a["retry"] for a in ledger] == list(range(calls))
        assert all(a["model"] == "fixed-chosen-model" and a["usage"]["input_tokens"] is None for a in ledger)
        summary = (await client.get(f"/api/games/{gid}/usage")).json()
        assert summary["unknown_usage_calls"] == calls


@pytest.mark.asyncio
async def test_manual_prepared_usage_restart_and_unchanged_connection_label(tmp_path):
    game = GameDouble()
    async with environment(tmp_path, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, _):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid)
        created = (await client.post("/api/account/games", headers=headers, json=body)).json()
        gid, token = created["game"]["game_id"], created["session_token"]
        runtime = app.state.service.games[gid]
        before = content_hash(runtime.engine.snapshot())
        await prepared(app, gid)
        buffered = deepcopy(runtime.prepared)
        assert content_hash(runtime.engine.snapshot()) == before
        assert len(game.calls) == 1
        await client.get(f"/api/games/{gid}/usage")
        await client.get(f"/api/games/{gid}/events")
        assert len(game.calls) == 1
    restarted = create_app(tmp_path / "game.sqlite3", game_factory=game, reading_scale=0)
    async with opened(restarted) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        runtime = restarted.state.service.games[gid]
        assert runtime.status == "paused" and runtime.prepared == buffered
        assert len(game.calls) == 1
        assert (await client.post(f"/api/games/{gid}/resume", json={"operation_id":"resume"})).status_code == 200
        state = (await client.get(f"/api/games/{gid}")).json()
        await asyncio.sleep(.01)
        assert len(game.calls) == 1
        assert (await advance(client, state, "continue")).status_code == 200
        async with asyncio.timeout(2):
            while runtime.engine.player_view(4)["phase"] == "night":
                await asyncio.sleep(.001)
        assert sum(r["seat"] == buffered["seat"] and r["action_kind"] == buffered["action"]["kind"]
                   for r in restarted.state.service.store.attempts(gid)) == 1


@pytest.mark.asyncio
async def test_logout_cancel_preserves_unknown_usage_reauthorization_and_abandon_retains_archive(tmp_path):
    game = GameDouble(hold=True)
    async with environment(tmp_path, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, _):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid)
        created = (await client.post("/api/account/games", headers=headers, json=body)).json()
        gid = created["game"]["game_id"]
        await game.started.wait()
        await client.post("/api/account/logout", headers=headers, json={})
        state = await ready(client, gid)
        assert state["status"] == "paused" and state["pause_reason"] == "account_session_expired"
        async with asyncio.timeout(2):
            while app.state.service.games[gid].worker:
                await asyncio.sleep(.001)
        assert app.state.service.store.attempts(gid)[0]["status"] == "cancelled"
        assert (await client.post(f"/api/games/{gid}/resume", json={"operation_id":"not-authorized"})).status_code == 403
        login = await client.post("/api/account/login", json={"username":"alice","password":"long-offline-password"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert (await client.post(f"/api/account/games/{gid}/authorize", headers=headers, json={"confirm_cost":True})).status_code == 200
        assert len(game.calls) == 1
        aborted = await client.post(f"/api/games/{gid}/abandon", json={"operation_id":"abandon"})
        assert aborted.json()["status"] == "abandoned"
        assert (await client.post(f"/api/games/{gid}/resume", json={"operation_id":"after-abandon"})).status_code == 409
        assert (await client.get(f"/api/games/{gid}/replay")).status_code == 409
        assert len(app.state.service.store.attempts(gid)) == 1
        assert (await client.get("/api/account/games/current")).json()["game"] is None
        assert (await client.post("/api/games", json={})).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [("model","configuration_changed"), ("key","configuration_changed"),
                                         ("parameters","configuration_changed"), ("delete","connection_unavailable")])
async def test_connection_changes_never_substitute_model_or_make_another_call(tmp_path, change, reason):
    game = GameDouble()
    async with environment(tmp_path, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, _):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid, human=1)
        created = (await client.post("/api/account/games", headers=headers, json=body)).json()
        gid = created["game"]["game_id"]
        assert not game.calls  # First actor is the human; no prefetch is possible.
        if change == "delete":
            assert (await client.post(f"/api/account/connections/{cid}/delete", headers=headers, json={"confirm":True})).status_code == 200
        else:
            config = (await client.get("/api/account/connections")).json()["connections"][0]
            update = {k: deepcopy(config[k]) for k in ("label","provider","model","generation")}
            if change == "model":
                update["model"] = "other-model"
            elif change == "key":
                update["api_key"] = "replacement-fake-key"
            else:
                update["generation"]["temperature"] = .2
            assert (await client.post(f"/api/account/connections/{cid}", headers=headers, json=update)).status_code == 200
        assert (await advance(client, created["game"], "initial-step")).status_code == 200
        state = await ready(client, gid)
        assert state["turn_version"]
        assert (await client.post(f"/api/games/{gid}/actions", json=submit_body(state))).status_code == 200
        state = await ready(client, gid)
        assert state["status"] == "paused" and state["pause_reason"] == reason
        assert not game.calls and app.state.service.store.attempts(gid) == []
        assert all(c.model == "fixed-chosen-model" for c in app.state.service.games[gid].connections)


@pytest.mark.asyncio
async def test_billed_failures_and_third_attempt_success_are_counted_without_cache_duplication(tmp_path):
    def fail(count):
        if count < 3:
            raise ProviderError("server_error", retryable=True, usage=ModelUsage(80,5,20,None))
    game = GameDouble(fail)
    async with environment(tmp_path, game_factory=game, game_retry_delay=0, reading_scale=0) as (app, client, _, headers, cid, _):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid)
        created = (await client.post("/api/account/games", headers=headers, json=body)).json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        ledger = app.state.service.store.attempts(gid)
        assert [a["status"] for a in ledger] == ["error","error","ok"]
        summary = (await client.get(f"/api/games/{gid}/usage")).json()
        assert summary["calls"] == 3 and summary["known_input_tokens"] == 260
        assert summary["known_output_tokens"] == 20 and summary["known_cached_input_tokens"] == 90
        assert summary["unknown_usage_calls"] == 0


@pytest.mark.asyncio
async def test_invalid_json_retries_keep_usage_and_same_context_then_pause(tmp_path):
    class Invalid(GameDouble):
        async def complete(self, request):
            self.calls.append(request)
            return ModelResponse("{}", ModelUsage(100,10), simulated=True)

    game = Invalid()
    async with environment(tmp_path, game_factory=game, game_retry_delay=0, reading_scale=0) as (app, client, _, headers, cid, _):
        await qualified(app, client, headers, cid)
        body = await start_body(client, headers, cid)
        created = (await client.post("/api/account/games", headers=headers, json=body)).json()
        gid = created["game"]["game_id"]
        state = await ready(client, gid)
        assert state["status"] == "paused" and state["pause_reason"] == "invalid_action"
        assert len(game.calls) == 3
        assert len({repr(c.messages) for c in game.calls}) == 1
        summary = (await client.get(f"/api/games/{gid}/usage")).json()
        assert summary["calls"] == 3 and summary["known_input_tokens"] == 300
        assert summary["known_output_tokens"] == 30 and summary["unknown_usage_calls"] == 0
