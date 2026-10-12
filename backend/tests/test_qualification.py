import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
import pytest
from app_helpers import flow_app as create_app

from werewolf.agent import ModelResponse, ModelUsage, parse_action
from werewolf.deepseek import DeepSeekAdapter, ProviderError
from werewolf.profiles import PersonaProfile
from werewolf.qualification import QualificationService, usage_summary
from werewolf.qualification_questions import questions
from werewolf.service import ServiceError

TEST_KEY = "fake-canary-key-never-use-real"
QUESTION_BY_ID = {q.id: q for q in questions()}


def correct(q):
    spec = q.view["legal_actions"][0]
    action = {"kind": spec["kind"]}
    if spec["kind"] == "witch":
        action.update(potion="skip", target=None)
    if "targets" in spec:
        action["target"] = q.expected_target if q.category == "logic" else spec["targets"][0]
    if "max_length" in spec:
        action["text"] = "只按可见证据与合法协议行动。"
    return action


class ScriptAdapter:
    def __init__(self, *, simulated=False, fail=None, wrong=(), hold=None):
        self.simulated, self.fail, self.wrong, self.hold = simulated, fail, wrong, hold
        self.calls = []

    async def complete(self, request):
        data = json.loads(request.messages[-1].content)
        question_id = data["qualification_task"]["id"]
        self.calls.append(request)
        if self.hold:
            await self.hold.wait()
        if self.fail:
            self.fail(len(self.calls))
        question = next(q for q in questions(data["state"]["rules"]) if q.id == question_id)
        action = correct(question)
        if question_id in self.wrong:
            if question_id.startswith("r"):
                action["unexpected"] = "invalid answer"
            else:
                action["target"] = None if action["target"] is not None else 2
        return ModelResponse(json.dumps(action), ModelUsage(100, 10, 60, None), simulated=self.simulated)


@asynccontextmanager
async def environment(tmp_path, adapter=None, **options):
    adapter = adapter or ScriptAdapter()
    app = create_app(tmp_path / "game.sqlite3", qualification_factory=lambda _: adapter,
                     qualification_retry_delay=0, qualification_enabled=options.pop("qualification_enabled", True), **options)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        registered = await client.post("/api/account/register", json={"username": "alice", "password": "long-offline-password"})
        user = registered.json()["user"]["id"]
        headers = {"X-CSRF-Token": registered.json()["csrf_token"]}
        saved = await client.post("/api/account/connections", headers=headers,
                                 json={"label": "test", "provider": "deepseek", "model": "fixed-chosen-model", "api_key": TEST_KEY})
        connection_id = saved.json()["id"]
        yield app, client, user, headers, connection_id, adapter


async def preview(client, headers, connection_id, persona=None):
    response = await client.post("/api/account/qualifications/preview", headers=headers,
                                json={"connection_id": connection_id, **({"persona": persona} if persona else {})})
    assert response.status_code == 200, response.text
    return response.json()


async def start(client, headers, connection_id, operation="start", *, force=False):
    before = await preview(client, headers, connection_id)
    body = {"connection_id": connection_id, "operation_id": operation, "confirm_cost": True,
            "expected_version": before["configuration_version"], "force_retest": force}
    response = await client.post("/api/account/qualifications", headers=headers, json=body)
    assert response.status_code == 202, response.text
    return response.json(), body


async def finish(app, client, run):
    task = app.state.qualifications.tasks.get(run["id"])
    if task:
        await task
    result = await client.get(f"/api/account/qualifications/{run['id']}")
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.asyncio
async def test_twelve_contexts_program_grading_durable_ledger_cache_and_idempotency(tmp_path):
    async with environment(tmp_path) as (app, client, user, headers, connection_id, adapter):
        initial = await preview(client, headers, connection_id)
        assert not initial["cached"] and len(initial["questions"]) == 12 and not adapter.calls
        run, body = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert report["qualified"] and report["rule_passed"] == report["logic_passed"] == 6
        assert len(adapter.calls) == report["usage"]["calls"] == 12
        assert report["usage"]["known_input_tokens"] == 1200
        assert report["usage"]["known_output_tokens"] == 120
        assert report["usage"]["known_cached_input_tokens"] == 720
        for call in adapter.calls:
            assert len(call.messages) == 2 and call.connection.model == "fixed-chosen-model"
            data = json.loads(call.messages[-1].content)
            assert "expected_target" not in data["qualification_task"] and TEST_KEY not in repr(call)
        assert TEST_KEY not in json.dumps(report) and "只按可见" not in json.dumps(report)
        assert app.state.service.games == {}
        cached_run, _ = await start(client, headers, connection_id, "cached")
        assert cached_run["id"] == run["id"] and len(adapter.calls) == 12
        for _ in range(3):
            repeated = await client.post("/api/account/qualifications", headers=headers, json=body)
            assert repeated.json()["id"] == run["id"]
            await client.get(f"/api/account/qualifications/{run['id']}")
        assert len(adapter.calls) == 12
        assert (await client.post("/api/account/qualifications", headers=headers,
                                  json={**body, "force_retest": True})).status_code == 409
        assert app.state.qualifications.require_qualified(user, connection_id, PersonaProfile())["id"] == run["id"]
        # There still is no paid game endpoint that bypasses admission.
        assert (await client.post("/api/games", json={"mode": "real"})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong,status,qualified", [(("r1",), "failed", False),
    (("l1",), "passed", True), (("l1", "l2"), "failed", False)])
async def test_thresholds_and_wrong_answers_are_not_corrected_or_retried(tmp_path, wrong, status, qualified):
    async with environment(tmp_path, ScriptAdapter(wrong=wrong)) as (app, client, _, headers, connection_id, adapter):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert report["status"] == status and report["qualified"] is qualified
        assert len(adapter.calls) == 12 and len(report["results"]) == 12
        assert all(a["retry"] == 0 for a in report["attempts"])


@pytest.mark.asyncio
async def test_simulated_pass_never_grants_real_qualification(tmp_path):
    async with environment(tmp_path, ScriptAdapter(simulated=True)) as (app, client, user, headers, connection_id, _):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert report["status"] == "passed" and report["simulated"] and not report["qualified"]
        with pytest.raises(ServiceError) as error:
            app.state.qualifications.require_qualified(user, connection_id, PersonaProfile())
        assert error.value.status == 403


@pytest.mark.asyncio
async def test_configuration_fingerprint_display_changes_persona_and_key_revision(tmp_path):
    async with environment(tmp_path) as (app, client, user, headers, connection_id, _):
        run, _ = await start(client, headers, connection_id)
        await finish(app, client, run)
        before = await preview(client, headers, connection_id)
        saved = (await client.get("/api/account/connections")).json()["connections"][0]
        body = {k: saved[k] for k in ("label", "provider", "model", "generation")}
        await client.post(f"/api/account/connections/{connection_id}", headers=headers, json={**body, "label": "renamed"})
        renamed = await preview(client, headers, connection_id)
        assert renamed["cached"] and renamed["fingerprint"] == before["fingerprint"]
        custom = await preview(client, headers, connection_id, asdict_persona("changed personality"))
        assert custom["fingerprint"] != before["fingerprint"] and not custom["cached"]
        await client.post(f"/api/account/connections/{connection_id}", headers=headers,
                          json={**body, "api_key": "replacement-fake-key"})
        replaced = await preview(client, headers, connection_id)
        assert replaced["fingerprint"] == before["fingerprint"] and not replaced["cached"]
        assert replaced["configuration_version"] != before["configuration_version"]
        with pytest.raises(ServiceError):
            app.state.qualifications.require_qualified(user, connection_id, PersonaProfile())
        assert not (await client.get(f"/api/account/qualifications/{run['id']}")).json()["qualified"]


def asdict_persona(description):
    return PersonaProfile(description=description).model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_calls", [("authentication", 1), ("balance", 1), ("timeout", 3), ("rate_limit", 3)])
async def test_transport_failure_limits_and_unknown_usage(tmp_path, failure, expected_calls):
    def fail(_):
        raise ProviderError(failure, retryable=failure in {"timeout", "rate_limit"})
    async with environment(tmp_path, ScriptAdapter(fail=fail)) as (app, client, _, headers, connection_id, adapter):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert report["status"] == "incomplete" and not report["qualified"]
        assert len(adapter.calls) == expected_calls == report["usage"]["calls"]
        assert report["usage"]["unknown_usage_calls"] == expected_calls
        assert all(a["usage"]["input_tokens"] is None for a in report["attempts"])


@pytest.mark.asyncio
async def test_retry_success_records_each_call_without_switching_model(tmp_path):
    def first_fails(number):
        if number == 1:
            raise ProviderError("timeout", retryable=True)
    async with environment(tmp_path, ScriptAdapter(fail=first_fails)) as (app, client, _, headers, connection_id, adapter):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert report["qualified"] and report["usage"]["calls"] == 13
        assert report["usage"]["unknown_usage_calls"] == 1
        assert [a["retry"] for a in report["attempts"][:2]] == [0, 1]
        assert len({c.connection.model for c in adapter.calls}) == 1


@pytest.mark.asyncio
async def test_explicit_confirmation_version_csrf_ownership_and_no_key_read_for_preview(tmp_path, monkeypatch):
    async with environment(tmp_path) as (app, client, _, headers, connection_id, adapter):
        monkeypatch.setattr(app.state.accounts, "credential", lambda *args: pytest.fail("must not decrypt"))
        before = await preview(client, headers, connection_id)
        body = {"connection_id": connection_id, "operation_id": "op", "expected_version": before["configuration_version"]}
        assert (await client.post("/api/account/qualifications", headers=headers, json=body)).status_code == 422
        assert (await client.post("/api/account/qualifications", json={**body, "confirm_cost": True})).status_code == 403
        assert (await client.post("/api/account/qualifications", headers=headers,
                                  json={**body, "confirm_cost": True, "expected_version": "stale"})).status_code == 409
        registered = (await client.post("/api/account/register", json={"username": "bob", "password": "long-offline-password"})).json()
        other_headers = {"X-CSRF-Token": registered["csrf_token"]}
        assert (await client.post("/api/account/qualifications/preview", headers=other_headers,
                                  json={"connection_id": connection_id})).status_code == 404
        assert not adapter.calls


@pytest.mark.asyncio
async def test_cancel_unknown_inflight_and_logout_stop_future_calls(tmp_path):
    hold = asyncio.Event()
    async with environment(tmp_path, ScriptAdapter(hold=hold)) as (app, client, _, headers, connection_id, adapter):
        run, _ = await start(client, headers, connection_id)
        while not adapter.calls:
            await asyncio.sleep(.001)
        second = await client.post("/api/account/qualifications", headers=headers,
                                   json={"connection_id": connection_id, "operation_id": "another", "confirm_cost": True,
                                         "expected_version": (await preview(client, headers, connection_id))["configuration_version"]})
        assert second.status_code == 409
        response = await client.post(f"/api/account/qualifications/{run['id']}/cancel", headers=headers, json={})
        assert response.json()["status"] == "cancelled"
        assert response.json()["attempts"][0]["status"] == "cancelled"
        assert response.json()["usage"]["unknown_usage_calls"] == 1
        again, _ = await start(client, headers, connection_id, "new")
        while len(adapter.calls) < 2:
            await asyncio.sleep(.001)
        assert (await client.post("/api/account/logout", headers=headers, json={})).status_code == 200
        assert app.state.qualifications.tasks[again["id"]].done()
        assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_forced_failed_retest_invalidates_old_pass_and_restart_restores_report(tmp_path):
    async with environment(tmp_path) as (app, client, _, headers, connection_id, adapter):
        passed, _ = await start(client, headers, connection_id)
        await finish(app, client, passed)
        adapter.wrong = ("r1",)
        failed, _ = await start(client, headers, connection_id, "retest", force=True)
        await finish(app, client, failed)
        assert not (await preview(client, headers, connection_id))["cached"]
        assert not (await client.get(f"/api/account/qualifications/{passed['id']}")).json()["qualified"]
        cookies = client.cookies
    app2 = create_app(tmp_path / "game.sqlite3")
    async with app2.router.lifespan_context(app2), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://localhost", cookies=cookies
    ) as client:
        restored = (await client.get(f"/api/account/qualifications/{failed['id']}")).json()
        assert restored["status"] == "failed" and restored["usage"]["calls"] == 12
        assert not app2.state.qualifications.tasks


def test_every_question_uses_legal_unique_grading_and_known_usage_is_not_zero_for_unknown():
    suite = questions()
    assert len(suite) == 12 and len({q.id for q in suite}) == 12
    for q in suite:
        action = parse_action(json.dumps(correct(q)), q.view["legal_actions"][0])
        if q.category == "logic":
            assert action.target == q.expected_target
    usage = usage_summary([{"usage": asdict(ModelUsage(100, 10, 60, 2))}, {"usage": asdict(ModelUsage())}])
    assert usage["known_input_tokens"] + usage["known_output_tokens"] == 110
    assert usage["unknown_usage_calls"] == 1 and usage["estimated_cost"] is None


@pytest.mark.asyncio
async def test_whole_exam_through_mock_http_and_no_answer_or_memory_in_requests(tmp_path):
    bodies = []

    def http(request):
        body = json.loads(request.content)
        bodies.append(body)
        data = json.loads(body["messages"][-1]["content"])
        q = next(q for q in questions(data["state"]["rules"]) if q.id == data["qualification_task"]["id"])
        assert "expected_target" not in data["qualification_task"]
        assert len(body["messages"]) == 2
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(correct(q)), "reasoning_content": "never-store-this-reasoning"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 20, "prompt_cache_hit_tokens": 150}})

    adapter = DeepSeekAdapter(TEST_KEY, transport=httpx.MockTransport(http), simulated=True)
    async with environment(tmp_path, adapter) as (app, client, _, headers, connection_id, _):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert len(bodies) == 12 and report["status"] == "passed" and not report["qualified"]
        assert report["usage"]["known_input_tokens"] == 2400 and report["usage"]["known_output_tokens"] == 240
        assert report["usage"]["known_cached_input_tokens"] == 1800
        assert report["usage"]["unknown_reasoning_usage_calls"] == 12
        assert "never-store-this-reasoning" not in json.dumps(report)


@pytest.mark.asyncio
async def test_connection_change_inflight_stops_next_call_and_reports_invalid(tmp_path):
    hold = asyncio.Event()
    async with environment(tmp_path, ScriptAdapter(hold=hold)) as (app, client, _, headers, connection_id, adapter):
        run, _ = await start(client, headers, connection_id)
        while not adapter.calls:
            await asyncio.sleep(.001)
        saved = (await client.get("/api/account/connections")).json()["connections"][0]
        body = {k: saved[k] for k in ("label", "provider", "model", "generation")}
        body["model"] = "a-different-model"
        await client.post(f"/api/account/connections/{connection_id}", headers=headers, json=body)
        hold.set()
        report = await finish(app, client, run)
        assert len(adapter.calls) == 1 and report["status"] == "incomplete"
        assert report["error"] == "configuration_changed" and not report["qualified"]


@pytest.mark.asyncio
async def test_cross_account_cannot_read_cancel_or_reuse_report(tmp_path):
    async with environment(tmp_path) as (app, client, _, headers, connection_id, adapter):
        run, body = await start(client, headers, connection_id)
        await finish(app, client, run)
        registered = (await client.post("/api/account/register", json={"username": "bob", "password": "long-offline-password"})).json()
        other_headers = {"X-CSRF-Token": registered["csrf_token"]}
        assert (await client.get(f"/api/account/qualifications/{run['id']}")).status_code == 404
        assert (await client.post(f"/api/account/qualifications/{run['id']}/cancel", json={}, headers=other_headers)).status_code == 404
        assert (await client.post("/api/account/qualifications", json=body, headers=other_headers)).status_code == 404
        assert len(adapter.calls) == 12


@pytest.mark.asyncio
async def test_restart_marks_inflight_unknown_and_does_not_resume(tmp_path):
    async with environment(tmp_path) as (app, client, _, headers, connection_id, _):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        # Trusted persisted crash fixture; no extra transport and no user database.
        report.update(status="running", qualified=False)
        report["attempts"][-1].update(status="in_flight", usage=asdict(ModelUsage()))
        app.state.qualifications.persist(report)
        # Construct recovery directly before normal shutdown so it sees the crash fixture.
        recovered = QualificationService(app.state.accounts)
        user_id = app.state.accounts.db.execute("SELECT user_id FROM qualifications WHERE id=?", (run["id"],)).fetchone()[0]
        after = recovered.report(user_id, run["id"])
        assert after["status"] == "incomplete" and after["error"] == "server_restart"
        assert after["attempts"][-1]["status"] == "interrupted"
        assert after["usage"]["unknown_usage_calls"] == 1 and recovered.tasks == {}
        await recovered.close()


@pytest.mark.asyncio
async def test_unexpected_adapter_error_is_sanitized_and_not_left_inflight(tmp_path):
    def fail(_):
        raise RuntimeError(f"Authorization: Bearer {TEST_KEY}")
    async with environment(tmp_path, ScriptAdapter(fail=fail)) as (app, client, _, headers, connection_id, _):
        run, _ = await start(client, headers, connection_id)
        report = await finish(app, client, run)
        assert report["status"] == "incomplete" and report["error"] == "internal_error"
        assert report["attempts"][0]["status"] == "error"
        assert TEST_KEY not in json.dumps(report)
