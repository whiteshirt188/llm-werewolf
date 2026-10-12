"""One consent, deterministic grouping, sequential calls and no restart/refresh spending."""

import asyncio

import httpx
import pytest
from app_helpers import flow_app as create_app
from test_qualification import ScriptAdapter, environment, finish, start
from test_table_plan import plan

from werewolf.deepseek import ProviderError
from werewolf.profiles import PersonaProfile


def nine(cid, different=False):
    table = plan(cid)
    table["roster"] = {"players":9,"wolves":3,"seers":1,"witches":1,"hunters":1}
    table["ai_profiles"] = [{"seat":s,"connection_id":cid,"persona":PersonaProfile().model_dump()} for s in range(2,10)]
    if different:
        table["ai_profiles"][-1]["persona"]["risk"] = "aggressive"
    return table


async def prepare(client,headers,table):
    response = await client.post("/api/account/qualification-batches/preview",headers=headers,json=table)
    assert response.status_code == 200,response.text
    return response.json()


async def begin(client,headers,table,preview,op="batch"):
    body = {"plan":table,"operation_id":op,"expected_version":preview["version"],"confirm_cost":True}
    response = await client.post("/api/account/qualification-batches",headers=headers,json=body)
    assert response.status_code == 202,response.text
    return response.json(),body


async def done(app,client,batch):
    await app.state.qualification_batches.tasks[batch["id"]]
    response = await client.get(f"/api/account/qualification-batches/{batch['id']}")
    assert response.status_code == 200
    return response.json()


async def called(adapter):
    for _ in range(200):
        if adapter.calls:
            return
        await asyncio.sleep(.005)
    pytest.fail("测试没有启动模拟调用")


@pytest.mark.asyncio
async def test_eight_seats_one_exam_and_idempotent_replay_refresh_never_spend(tmp_path):
    async with environment(tmp_path) as (app,client,_,headers,cid,adapter):
        table = nine(cid)
        before = await prepare(client,headers,table)
        assert before["normal_calls"] == 13 and before["maximum_calls"] == 39
        assert len(before["groups"]) == 1 and before["groups"][0]["seats"] == list(range(2,10))
        assert not adapter.calls
        run,body = await begin(client,headers,table,before)
        result = await done(app,client,run)
        assert result["status"] == "completed" and result["groups"][0]["exam"]["qualified"]
        assert result["usage"]["calls"] == 13 and len(adapter.calls) == 13
        assert (await client.post("/api/account/qualification-batches",headers=headers,json=body)).json()["id"] == run["id"]
        assert (await client.get("/api/account/qualification-batches/current")).json()["batch"]["id"] == run["id"]
        assert (await prepare(client,headers,table))["normal_calls"] == 0
        assert len(adapter.calls) == 13
        check = await client.post("/api/account/table-plan/admission",headers=headers,json=table)
        assert check.json()["all_qualified"]
        assert (await client.post("/api/account/qualification-batches",headers=headers,json={**body,"plan":nine(cid,True)})).status_code == 409


@pytest.mark.asyncio
async def test_distinct_personas_are_two_sequential_exams_not_eight(tmp_path):
    adapter = ScriptAdapter(hold=asyncio.Event())
    async with environment(tmp_path,adapter) as (app,client,_,headers,cid,_):
        table = nine(cid,True)
        before = await prepare(client,headers,table)
        assert len(before["groups"]) == 2 and before["normal_calls"] == 26
        run,_ = await begin(client,headers,table,before)
        await called(adapter)
        current = (await client.get(f"/api/account/qualification-batches/{run['id']}")).json()
        assert [g["status"] for g in current["groups"]] == ["running","pending"]
        assert len(adapter.calls) == 1 and current["usage"]["unknown_usage_calls"] == 1
        adapter.hold.set()
        result = await done(app,client,run)
        assert all(g["exam"]["qualified"] for g in result["groups"])
        assert len(adapter.calls) == 26


@pytest.mark.asyncio
async def test_cached_groups_are_skipped_and_not_added_to_batch_usage(tmp_path):
    async with environment(tmp_path) as (app,client,_,headers,cid,adapter):
        run,_ = await start(client,headers,cid)
        await finish(app,client,run)
        table = plan(cid)
        table["ai_profiles"][-1]["persona"]["risk"] = "aggressive"
        before = await prepare(client,headers,table)
        assert [g["cached"] for g in before["groups"]] == [True,False]
        assert before["normal_calls"] == 12
        batch,_ = await begin(client,headers,table,before)
        result = await done(app,client,batch)
        assert result["usage"]["calls"] == 12 and len(adapter.calls) == 24


@pytest.mark.asyncio
async def test_explicit_consent_csrf_stale_and_unsupported_reject_without_calls(tmp_path):
    async with environment(tmp_path) as (_,client,_,headers,cid,adapter):
        table = nine(cid)
        preview = await prepare(client,headers,table)
        body = {"plan":table,"operation_id":"no-spend","expected_version":preview["version"],"confirm_cost":True}
        assert (await client.post("/api/account/qualification-batches",json=body)).status_code == 403
        assert (await client.post("/api/account/qualification-batches",headers=headers,json={**body,"confirm_cost":False})).status_code == 422
        assert (await client.post("/api/account/qualification-batches",headers=headers,json={**body,"expected_version":"stale"})).status_code == 409
        silicon = (await client.post("/api/account/connections",headers=headers,json={"label":"unsupported","provider":"siliconflow","model":"fake","api_key":"fake"})).json()["id"]
        unsupported = nine(silicon)
        before = await prepare(client,headers,unsupported)
        assert before["blocked"]
        body.update(plan=unsupported,expected_version=before["version"])
        assert (await client.post("/api/account/qualification-batches",headers=headers,json=body)).status_code == 422
        assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("logout",[False,True])
async def test_cancel_or_logout_stops_whole_queue_and_individual_cannot_interleave(tmp_path,logout):
    adapter = ScriptAdapter(hold=asyncio.Event())
    async with environment(tmp_path,adapter) as (app,client,user,headers,cid,_):
        table = nine(cid,True)
        batch,_ = await begin(client,headers,table,await prepare(client,headers,table))
        await called(adapter)
        preview = (await client.post("/api/account/qualifications/preview",headers=headers,json={"connection_id":cid})).json()
        response = await client.post("/api/account/qualifications",headers=headers,json={"connection_id":cid,"operation_id":"individual","expected_version":preview["configuration_version"],"confirm_cost":True})
        assert response.status_code == 409
        if logout:
            assert (await client.post("/api/account/logout",headers=headers,json={})).status_code == 200
            result = app.state.qualification_batches.report(user,batch["id"])
        else:
            response = await client.post(f"/api/account/qualification-batches/{batch['id']}/cancel",headers=headers,json={})
            assert response.status_code == 200
            result = response.json()
        assert result["status"] == "cancelled"
        assert len(adapter.calls) == 1 and result["usage"]["unknown_usage_calls"] == 1
        assert result["groups"][1]["status"] == "pending"
        assert app.state.qualifications.batch_reservation is None


@pytest.mark.asyncio
async def test_configuration_changes_abort_queue_and_ownership_is_enforced(tmp_path):
    adapter = ScriptAdapter(hold=asyncio.Event())
    async with environment(tmp_path,adapter) as (app,client,_,headers,cid,_):
        table = nine(cid,True)
        run,_ = await begin(client,headers,table,await prepare(client,headers,table))
        await called(adapter)
        edited = await client.post(f"/api/account/connections/{cid}",headers=headers,json={"label":"changed","provider":"deepseek","model":"other-model"})
        assert edited.status_code == 200,edited.text
        adapter.hold.set()
        result = await done(app,client,run)
        assert result["status"] == "incomplete" and result["error"] == "configuration_changed"
        assert len(adapter.calls) == 1
        await client.post("/api/account/register",json={"username":"bob","password":"long-offline-password"})
        assert (await client.get(f"/api/account/qualification-batches/{run['id']}")).status_code == 404
        assert (await client.get("/api/account/qualification-batches/current")).json()["batch"] is None


@pytest.mark.asyncio
async def test_provider_auth_failure_stops_remaining_groups_without_retry(tmp_path):
    def fail(_):
        raise ProviderError("authentication")
    async with environment(tmp_path,ScriptAdapter(fail=fail)) as (app,client,_,headers,cid,adapter):
        table = nine(cid,True)
        run,_ = await begin(client,headers,table,await prepare(client,headers,table))
        result = await done(app,client,run)
        assert result["status"] == "incomplete" and result["error"] == "authentication"
        assert len(adapter.calls) == 1 and result["groups"][1]["status"] == "pending"


@pytest.mark.asyncio
async def test_failed_answers_do_not_retry_or_prevent_other_groups_from_being_tested(tmp_path):
    async with environment(tmp_path,ScriptAdapter(wrong=("r_hunter",))) as (app,client,_,headers,cid,adapter):
        table = nine(cid,True)
        run,_ = await begin(client,headers,table,await prepare(client,headers,table))
        result = await done(app,client,run)
        assert result["status"] == "completed" and all(g["status"] == "failed" for g in result["groups"])
        assert len(adapter.calls) == 26
        assert not (await client.post("/api/account/table-plan/admission",headers=headers,json=table)).json()["all_qualified"]


@pytest.mark.asyncio
async def test_restart_restores_report_but_never_resumes_or_reveals_cookie(tmp_path):
    adapter = ScriptAdapter(hold=asyncio.Event())
    async with environment(tmp_path,adapter) as (app,client,_,headers,cid,_):
        table = nine(cid,True)
        _run,_ = await begin(client,headers,table,await prepare(client,headers,table))
        await called(adapter)
        cookies = httpx.Cookies(client.cookies)
        token = client.cookies.get("werewolf_account")
        assert token not in app.state.qualification_batches.vault.db.execute("SELECT payload FROM qualification_batches").fetchone()[0]
    adapter2 = ScriptAdapter()
    app2 = create_app(tmp_path / "game.sqlite3",qualification_factory=lambda _:adapter2)
    async with app2.router.lifespan_context(app2),httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),base_url="http://localhost",cookies=cookies) as client:
        result = (await client.get("/api/account/qualification-batches/current")).json()["batch"]
        assert result["status"] == "incomplete" and result["error"] == "server_shutdown"
        assert result["usage"]["unknown_usage_calls"] == 1 and not adapter2.calls
        assert not app2.state.qualification_batches.tasks
