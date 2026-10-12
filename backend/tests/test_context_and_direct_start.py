"""No real network/keys: default no-exam starts and actual adapter message injection."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import httpx
import pytest
from app_helpers import flow_app as create_app
from test_api import opened, submit_body
from test_deepseek import envelope
from test_manual import prepared
from test_qualification import environment
from test_real_games import GameDouble
from test_table_plan import plan

from werewolf import GameEngine, deepseek, siliconflow
from werewolf.agent import ModelResponse, PlayerAgent
from werewolf.demo import simulated_action
from werewolf.profiles import PersonaProfile, SeatProfile, canonical_json
from werewolf.prompts import PromptBuilder
from werewolf.providers import adapter_version
from werewolf.roster import RosterConfig
from werewolf.rulebook import Rulebook, RulebookError


async def admission(client, headers, table):
    response = await client.post("/api/account/table-plan/admission", headers=headers, json=table)
    assert response.status_code == 200, response.text
    return response.json()


def start_body(table, report, operation="direct-start"):
    return {"plan": table, "confirm_cost": True, "operation_id": operation,
            "expected_versions": {str(s["seat"]): s["configuration_version"] for s in report["seats"]}}


@pytest.mark.asyncio
async def test_production_default_cancels_exams_even_direct_service_calls(tmp_path):
    app = create_app(tmp_path / "default.sqlite3", qualification_factory=lambda _: pytest.fail("no exam adapter"))
    async with opened(app) as client:
        r = (await client.post("/api/account/register", json={"username":"alice", "password":"offline-long-password"})).json()
        headers = {"X-CSRF-Token":r["csrf_token"]}
        assert app.state.qualifications.enabled is False
        assert (await client.get("/api/health")).json()["qualification_required"] is False
        for path, body in [
            ("/qualifications/preview", {"connection_id":"unused"}),
            ("/qualifications", {"connection_id":"unused", "operation_id":"old-exam",
                                 "expected_version":"unused", "confirm_cost":True}),
            ("/qualification-batches/preview", plan("unused")),
            ("/qualification-batches", {"plan":plan("unused"), "operation_id":"old-batch",
                                       "expected_version":"unused", "confirm_cost":True}),
        ]:
            assert (await client.post("/api/account" + path, headers=headers, json=body)).status_code == 410
        assert not app.state.qualifications.tasks and not app.state.qualification_batches.tasks
        assert app.state.accounts.db.execute("SELECT count(*) FROM qualifications").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_no_exam_restart_keeps_frozen_context_and_buffer_without_recalls(tmp_path):
    game = GameDouble()
    async with environment(tmp_path, qualification_enabled=False, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, _):
        table = plan(cid)
        table["human_seat"] = 4
        table["ai_profiles"] = [{"seat":s, "nickname":str(s), "connection_id":cid,
                                 "persona":PersonaProfile().model_dump()} for s in range(1, 7) if s != 4]
        body = start_body(table, await admission(client, headers, table))
        response = await client.post("/api/account/games", headers=headers, json=body)
        assert response.status_code == 201, response.text
        created = response.json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        runtime = app.state.service.games[gid]
        buffer = deepcopy(runtime.prepared)
        frozen = runtime.book.files
        cookies = httpx.Cookies(client.cookies)
        assert len(game.calls) == 1 and runtime.real_binding["qualification_required"] is False
    restarted = create_app(tmp_path / "game.sqlite3", game_factory=game, reading_scale=0)
    async with opened(restarted) as client:
        client.cookies.update(cookies)
        client.headers["Authorization"] = f"Bearer {created['session_token']}"
        runtime = restarted.state.service.games[gid]
        assert runtime.status == "paused" and runtime.prepared == buffer and runtime.book.files == frozen
        assert restarted.state.qualifications.enabled is False
        for _ in range(3):
            assert (await client.get(f"/api/games/{gid}")).status_code == 200
            assert (await client.get("/api/account/games/current")).json()["game"]["game_id"] == gid
        repeated = await client.post("/api/account/games", headers=headers, json=body)
        assert repeated.status_code == 201 and repeated.json()["game"]["game_id"] == gid
        assert len(game.calls) == 1
        assert restarted.state.accounts.db.execute("SELECT count(*) FROM qualifications").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_config_preflight_never_decrypts_or_builds_requests_and_stale_start_rejected(tmp_path, monkeypatch):
    game = GameDouble()
    async with environment(tmp_path, qualification_enabled=False, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, exam):
        table = plan(cid)
        monkeypatch.setattr(app.state.accounts, "credential", lambda *_: pytest.fail("preflight decrypted key"))
        monkeypatch.setattr(app.state.qualifications, "configuration", lambda *_: pytest.fail("exam configured"))
        report = await admission(client, headers, table)
        assert report["all_ready"] and report["game_start_enabled"]
        assert not report["all_qualified"] and not report["qualification_required"]
        assert all(s["ready"] and s["report_id"] is None for s in report["seats"])
        assert not game.calls and not exam.calls
        missing = await admission(client, headers, plan("foreign-or-deleted"))
        assert not missing["all_ready"] and all(s["model"] is None for s in missing["seats"])
        body = start_body(table, report)
        assert (await client.post("/api/account/games", headers=headers, json={**body,"confirm_cost":False})).status_code == 422
        assert (await client.post("/api/account/games", json=body)).status_code == 403
        changed = deepcopy(body)
        changed["plan"]["ai_profiles"][0]["persona"]["bluff"] = "low"
        assert (await client.post("/api/account/games", headers=headers, json=changed)).status_code == 409
        response = await client.post(f"/api/account/connections/{cid}", headers=headers,
                                    json={"label":"new", "provider":"deepseek", "model":"changed"})
        assert response.status_code == 200
        assert (await client.post("/api/account/games", headers=headers, json=body)).status_code == 409
        assert not game.calls and not exam.calls and not app.state.service.games


@pytest.mark.asyncio
@pytest.mark.parametrize("settings,error", [
    ({"model":"Pro/zai-org/GLM-5.3"}, "unsupported_model"),
    ({"generation":{"reasoning_budget":128}}, "unsupported_reasoning_budget"),
    ({"generation":{"target_output_tokens":65537}}, "invalid_output_limit"),
])
async def test_unsupported_sf_config_rejected_without_call(tmp_path, settings, error):
    async with environment(tmp_path, qualification_enabled=False) as (app, client, _, headers, _, exam):
        c = (await client.post("/api/account/connections", headers=headers, json={
            "label":"GLM", "provider":"siliconflow", "model":siliconflow.GLM_MODEL,
            "api_key":"fake-key", **settings})).json()
        table = plan(c["id"])
        report = await admission(client, headers, table)
        assert not report["all_ready"] and all(s["error"] == error for s in report["seats"])
        assert (await client.post("/api/account/games", headers=headers, json=start_body(table, report))).status_code == 422
        assert not exam.calls and not app.state.service.games


@pytest.mark.asyncio
@pytest.mark.parametrize("players", [6, 9])
async def test_mixed_no_exam_games_send_skill_role_and_phase_to_actual_http_payload(tmp_path, monkeypatch, players):
    sent = []
    book = Rulebook.load()
    originals = {"deepseek":deepseek.DeepSeekAdapter, "siliconflow":siliconflow.SiliconFlowAdapter}

    def factory(provider):
        def handler(request):
            payload = json.loads(request.content)
            user = json.loads(payload["messages"][-1]["content"])
            state = user["state"]
            if user.get("task") == "opening_plan":
                selected = book.select(state["role"], "opening_plan", state["rules"])
                assert all(text in payload["messages"][0]["content"] for _, text in selected)
                assert "legal_actions" not in state and payload["max_tokens"] <= 256
                assert all(set(p) == {"seat", "alive"} for p in state["players"])
                sent.append((provider, payload["model"], state["seat"], "opening_plan"))
                return httpx.Response(200, json=envelope(canonical_json({"intent": "先观察再决定怎么打"})))
            spec = state["legal_actions"][0]
            selected = book.select(state["role"], spec["kind"], state["rules"])
            system = payload["messages"][0]["content"]
            assert len(selected) == 3
            assert all(text in system for _, text in selected)  # Entire core/role/phase, not just a filename.
            assert user["communication"]["action"] == spec["kind"]
            if spec["kind"] in {"speak", "last_words"}:
                assert user["communication"]["audience"] == "public"
            if spec["kind"] in {"propose_kill", "wolf_message"}:
                assert user["communication"]["audience"] == "wolf_team"
            for event in user["facts"] + user["history"]:
                if event["kind"].startswith("wolf_") or event["kind"] in {"inspection","guard_action","witch_action"}:
                    assert event["visibility"] == "private"
            if state["role"] != "wolf":
                assert state["wolf_teammates"] == []
                assert not any(e["kind"].startswith("wolf_") for e in user["facts"] + user["history"])
            assert "qualification_task" not in user and "fake-key" not in system
            assert all(set(p) == {"seat","alive"} for p in state["players"])
            sent.append((provider, payload["model"], state["seat"], spec["kind"]))
            action = asdict(simulated_action(state))
            schema = json.loads(system.split("JSON 对象：\n")[-1])
            fields = schema["properties"]["action"]["required"]
            result = {"action": {k:action[k] for k in fields}, "player_update": {}}
            assert "player_state" in user and "play_style" in user["persona"]
            return httpx.Response(200, json=envelope(canonical_json(result)))
        return lambda key: originals[provider](key, transport=httpx.MockTransport(handler), simulated=True)

    monkeypatch.setattr(deepseek, "DeepSeekAdapter", factory("deepseek"))
    monkeypatch.setattr(siliconflow, "SiliconFlowAdapter", factory("siliconflow"))
    async with environment(tmp_path, qualification_enabled=False, opening_enabled=True, reading_scale=0) as (app, client, _, headers, cid, exam):
        sf_ids = {}
        for model in siliconflow.SUPPORTED_MODELS:
            c = (await client.post("/api/account/connections", headers=headers, json={
                "label":model, "provider":"siliconflow", "model":model, "api_key":"fake-key"})).json()
            sf_ids[model] = c["id"]
        table = plan(cid)
        table.update(roster={"players":players,"wolves":2 if players == 6 else 3,"hunters":0 if players == 6 else 1}, playback_mode="auto")
        connections = [cid, sf_ids[siliconflow.HUNYUAN_MODEL], sf_ids[siliconflow.GLM_MODEL]]
        table["ai_profiles"] = [{"seat":s, "nickname":str(s), "connection_id":connections[s % 3],
                                 "persona":PersonaProfile().model_dump()} for s in range(2, players + 1)]
        report = await admission(client, headers, table)
        assert report["all_ready"] and not sent and not exam.calls
        body = start_body(table, report)
        response = await client.post("/api/account/games", headers=headers, json=body)
        assert response.status_code == 201, response.text
        gid = response.json()["game"]["game_id"]
        for _ in range(100):
            async with asyncio.timeout(5):
                while True:
                    state = (await client.get(f"/api/games/{gid}")).json()
                    if state["status"] != "running" or state["turn_version"]:
                        break
                    await asyncio.sleep(.001)
            if state["status"] == "finished":
                break
            assert state["status"] == "running", state
            assert (await client.post(f"/api/games/{gid}/actions", json=submit_body(state))).status_code == 200
        else:
            pytest.fail("offline direct-start game did not complete")
        assert sum(kind == "opening_plan" for _,_,_,kind in sent) == players - 1
        assert {model for _,model,_,_ in sent} == {"fixed-chosen-model", *siliconflow.SUPPORTED_MODELS}
        models = ["fixed-chosen-model", *siliconflow.SUPPORTED_MODELS]
        assert all(model == models[seat % 3] for _,model,seat,_ in sent)
        assert not exam.calls
        before = len(sent)
        assert (await client.post("/api/account/games", headers=headers, json=body)).json()["game"]["game_id"] == gid
        await client.get(f"/api/games/{gid}/replay")
        await client.get(f"/api/games/{gid}/events")
        usage = (await client.get(f"/api/games/{gid}/usage")).json()
        assert usage["calls"] == before == len(sent)
        assert app.state.accounts.db.execute("SELECT count(*) FROM qualifications").fetchone()[0] == 0


def test_public_wolf_request_keeps_private_information_labelled_not_removed():
    from werewolf.profiles import ModelConnection
    game = GameEngine(38)
    while game.player_view(1)["phase"] != "day_speech":
        if game.finished:
            pytest.fail("fixture finished before speech")
        seat = game.next_actor()
        game.submit(seat, simulated_action(game.player_view(seat)))
    # Seat 1 in seed 38 is wolf; daytime sends the same legal private knowledge for strategy.
    view = game.player_view(1)
    assert view["role"] == "wolf" and view["legal_actions"]
    c = ModelConnection(id="sf", provider="siliconflow", model=siliconflow.GLM_MODEL,
                        base_url=siliconflow.OFFICIAL_URL, adapter_version=adapter_version("siliconflow", siliconflow.GLM_MODEL))
    request = PromptBuilder(Rulebook.load()).build(SeatProfile(seat=1, connection_id=c.id), c, view)
    data = json.loads(request.messages[-1].content)
    assert data["communication"]["audience"] == "public"
    assert data["state"]["wolf_teammates"] == view["wolf_teammates"]
    assert any(e["kind"] == "wolf_decision" and e["visibility"] == "private" for e in data["facts"])
    assert data["decision_context"]["living_seats"] == [p["seat"] for p in view["players"] if p["alive"]]


@pytest.mark.asyncio
async def test_every_role_reads_frozen_skill_on_each_agent_decision():
    from werewolf.profiles import ModelConnection
    book = Rulebook.load()
    connection = ModelConnection(id="mock", provider="mock", model="fixed", base_url="mock://local")
    seen = set()

    class Capture:
        async def complete(self, request):
            data = json.loads(request.wire_messages()[-1]["content"])
            state = data["state"]
            spec = state["legal_actions"][0]
            assert all(text in request.wire_messages()[0]["content"] for _,text in book.select(state["role"], spec["kind"], state["rules"]))
            seen.add(state["role"])
            action = asdict(simulated_action(state))
            required = json.loads(request.response_schema)["required"]
            return ModelResponse(canonical_json({k:action[k] for k in required}), simulated=True)

    for extra in ({"witches":1,"guards":1}, {"witches":0,"hunters":1,"knights":1}):
        config = RosterConfig(players=9,wolves=3,**extra).engine_config()
        for seed in range(3):
            engine = GameEngine(seed, config)
            agents = {seat:PlayerAgent(SeatProfile(seat=seat,connection_id=connection.id), connection, PromptBuilder(book)) for seat in range(1,10)}
            for _ in range(300):
                if engine.finished:
                    break
                seat = engine.next_actor()
                engine.submit(seat, await agents[seat].decide(engine.player_view(seat), Capture()))
            assert engine.finished
    assert seen == {"wolf","villager","seer","witch","hunter","guard","knight"}


def test_missing_frozen_skill_fails_before_model_request():
    book = Rulebook.load()
    with pytest.raises(RulebookError):
        Rulebook(tuple((name,text) for name,text in book.files if name != "游戏.md")).select("wolf","speak")
