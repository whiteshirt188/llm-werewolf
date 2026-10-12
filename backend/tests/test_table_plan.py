"""Draft/admission checks use fake credentials and a local scripted adapter only."""

from copy import deepcopy

import httpx
import pytest
from app_helpers import flow_app as create_app
from test_qualification import ScriptAdapter, environment, finish, start

from werewolf.profiles import PersonaProfile
from werewolf.rulebook import Rulebook, RulebookError
from werewolf.service import ServiceError
from werewolf.table_plan import TablePlan, require_plan_qualified


def plan(connection_id):
    return {"human_seat": 1, "seed": 38, "playback_mode": "manual",
            "ai_profiles": [{"seat": s, "nickname": f"玩家 {s}", "connection_id": connection_id,
                             "persona": PersonaProfile().model_dump()} for s in range(2, 7)]}


async def check(client, headers, body):
    response = await client.post("/api/account/table-plan/admission", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["model_calls"] == 0 and result["game_start_enabled"] == result["all_qualified"]
    return result


@pytest.mark.asyncio
async def test_draft_auth_csrf_ownership_validation_and_optimistic_replay(tmp_path):
    async with environment(tmp_path) as (app, client, _, headers, connection_id, adapter):
        body = plan(connection_id)
        assert (await client.post("/api/account/table-plan", json={"plan": body})).status_code == 403
        assert (await client.post("/api/account/table-plan/admission", json=body)).status_code == 403
        before = await client.get("/api/account/table-plan")
        assert before.json() == {"version": None, "plan": None}
        assert before.headers["cache-control"] == "no-store"
        saved = await client.post("/api/account/table-plan", headers=headers, json={"plan": body})
        assert saved.status_code == 200
        version = saved.json()["version"]
        replay = await client.post("/api/account/table-plan", headers=headers, json={"plan": body})
        assert replay.json() == saved.json()  # Lost response replay is not another revision.
        changed = deepcopy(body)
        changed["ai_profiles"][0]["nickname"] = "另一窗口"
        assert (await client.post("/api/account/table-plan", headers=headers, json={"plan": changed})).status_code == 409
        updated = await client.post("/api/account/table-plan", headers=headers,
                                    json={"plan": changed, "expected_version": version})
        assert updated.status_code == 200 and updated.json()["version"] != version
        assert (await client.post("/api/account/table-plan", headers=headers,
                                  json={"plan": body, "expected_version": version})).status_code == 409
        for bad in [{**body, "ai_profiles": body["ai_profiles"][:4]},
                    {**body, "human_seat": 2}, {**body, "seed": 2**53}, {**body, "seed": True},
                    {**body, "qualified": True}, {**body, "ai_profiles": [body["ai_profiles"][0]] * 5}]:
            assert (await client.post("/api/account/table-plan", headers=headers, json={"plan": bad})).status_code == 422
        assert not adapter.calls and app.state.service.games == {}
        foreign = plan("unknown-or-foreign")
        assert (await client.post("/api/account/table-plan", headers=headers,
                                  json={"plan": foreign, "expected_version": updated.json()["version"]})).status_code == 404
        invalid = await check(client, headers, foreign)
        assert all(s["status"] == "unavailable" and s["model"] is None for s in invalid["seats"])


@pytest.mark.asyncio
async def test_shared_qualification_distinct_personas_and_current_start_boundary(tmp_path, monkeypatch):
    async with environment(tmp_path) as (app, client, user, headers, connection_id, adapter):
        body = plan(connection_id)
        no_exam = await check(client, headers, body)
        assert not no_exam["all_qualified"] and not adapter.calls
        with pytest.raises(ServiceError):
            require_plan_qualified(app.state.qualifications, user, TablePlan(**body))
        run, _ = await start(client, headers, connection_id)
        await finish(app, client, run)
        calls = len(adapter.calls)
        # Preflight must never cross the trusted credential boundary.
        monkeypatch.setattr(app.state.accounts, "credential", lambda *_: pytest.fail("preflight decrypted a key"))
        service = app.state.qualifications
        original = service.preview
        preview_calls = []

        def counted(*args):
            preview_calls.append(args)
            return original(*args)

        monkeypatch.setattr(service, "preview", counted)
        valid = await check(client, headers, body)
        assert valid["all_qualified"] and valid["qualification_configurations"] == valid["unique_connections"] == 1
        assert len(preview_calls) == 1
        assert {s["report_id"] for s in valid["seats"]} == {run["id"]}
        assert require_plan_qualified(service, user, TablePlan(**body))["all_qualified"]
        body["ai_profiles"][0]["nickname"] = "展示变化"
        body["ai_profiles"][0]["persona"]["label"] = "显示标签变化"
        assert (await check(client, headers, body))["all_qualified"]
        body["ai_profiles"][0]["persona"]["description"] = "另一种独立个性"
        distinct = await check(client, headers, body)
        assert distinct["qualification_configurations"] == 2
        assert sum(s["qualified"] for s in distinct["seats"]) == 4
        with pytest.raises(ServiceError) as error:
            require_plan_qualified(service, user, TablePlan(**body))
        assert error.value.status == 403
        assert len(adapter.calls) == calls == 12 and app.state.service.games == {}
        assert (await client.post("/api/games", json={"mode": "real"})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["key", "parameters", "model", "latest_failure", "simulated"])
async def test_current_admission_rejects_stale_and_simulated_qualification(tmp_path, change):
    adapter = ScriptAdapter(simulated=change == "simulated")
    async with environment(tmp_path, adapter) as (app, client, user, headers, connection_id, _):
        body = plan(connection_id)
        run, _ = await start(client, headers, connection_id)
        await finish(app, client, run)
        if change == "latest_failure":
            adapter.wrong = ("r1",)
            failed, _ = await start(client, headers, connection_id, "retest", force=True)
            await finish(app, client, failed)
        elif change != "simulated":
            config = (await client.get("/api/account/connections")).json()["connections"][0]
            update = {k: deepcopy(config[k]) for k in ("label", "provider", "model", "generation")}
            if change == "key":
                update["api_key"] = "replacement-fake-key"
            elif change == "parameters":
                update["generation"]["temperature"] = .2
            else:
                update["model"] = "another-fixed-model"
            assert (await client.post(f"/api/account/connections/{connection_id}", headers=headers, json=update)).status_code == 200
        checked = await check(client, headers, body)
        assert not checked["all_qualified"] and not any(s["qualified"] for s in checked["seats"])
        with pytest.raises(ServiceError):
            require_plan_qualified(app.state.qualifications, user, TablePlan(**body))


@pytest.mark.asyncio
async def test_cross_account_deleted_and_unsupported_connections_fail_closed(tmp_path):
    async with environment(tmp_path) as (_app, client, _, headers, connection_id, adapter):
        original = plan(connection_id)
        await client.post("/api/account/table-plan", headers=headers, json={"plan": original})
        silicon = await client.post("/api/account/connections", headers=headers,
                                   json={"label": "pending", "provider": "siliconflow", "model": "pending", "api_key": "fake"})
        unsupported = await check(client, headers, plan(silicon.json()["id"]))
        assert all(s["status"] == "unsupported" for s in unsupported["seats"])
        assert not adapter.calls
        await client.post(f"/api/account/connections/{connection_id}/delete", headers=headers, json={"confirm": True})
        assert (await client.get("/api/account/table-plan")).json()["plan"] == {
            **original, "roster": {"players": 6, "wolves": 2, "seers": 1, "witches": 1, "hunters": 0, "guards": 0, "knights": 0, "last_words": True}}
        deleted = await check(client, headers, original)
        assert all(s["status"] == "unavailable" for s in deleted["seats"])
        assert (await client.post("/api/account/table-plan", headers=headers, json={"plan": original})).status_code == 404
        bob = await client.post("/api/account/register", json={"username": "bob", "password": "second-offline-password"})
        bob_headers = {"X-CSRF-Token": bob.json()["csrf_token"]}
        assert (await client.get("/api/account/table-plan")).json()["plan"] is None
        foreign = await check(client, bob_headers, plan(silicon.json()["id"]))
        assert all(s["model"] is None and s["status"] == "unavailable" for s in foreign["seats"])
        await client.post("/api/account/logout", headers=bob_headers, json={})
        assert (await client.get("/api/account/table-plan")).status_code == 401


@pytest.mark.asyncio
async def test_restart_restores_only_account_draft_and_does_not_create_agents(tmp_path):
    async with environment(tmp_path) as (_, client, _, headers, connection_id, _):
        response = await client.post("/api/account/table-plan", headers=headers, json={"plan": plan(connection_id)})
        saved = response.json()
    app = create_app(tmp_path / "game.sqlite3", qualification_factory=lambda _: pytest.fail("unexpected adapter"), qualification_enabled=True)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        login = await client.post("/api/account/login", json={"username": "alice", "password": "long-offline-password"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert (await client.get("/api/account/table-plan")).json() == saved
        assert not (await check(client, headers, saved["plan"]))["all_qualified"]
        assert app.state.service.games == {}


@pytest.mark.asyncio
async def test_rulebook_unavailable_never_reuses_a_previous_pass(tmp_path, monkeypatch):
    async with environment(tmp_path) as (app, client, user, headers, connection_id, adapter):
        run, _ = await start(client, headers, connection_id)
        await finish(app, client, run)

        def broken():
            raise RulebookError("isolated missing Skill")

        monkeypatch.setattr(Rulebook, "load", broken)
        report = await check(client, headers, plan(connection_id))
        assert not report["all_qualified"]
        assert all(s["status"] == "unavailable" and s["configuration_version"] is None for s in report["seats"])
        with pytest.raises(ServiceError):
            require_plan_qualified(app.state.qualifications, user, TablePlan(**plan(connection_id)))
        assert len(adapter.calls) == 12
