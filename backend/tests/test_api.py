import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from app_helpers import flow_app as create_app

from werewolf.decision_budget import V10_BUDGET_POLICY
from werewolf.demo import simulated_action
from werewolf.mock_adapter import MockAdapter
from werewolf.profiles import canonical_json
from werewolf.rulebook import RulebookError
from werewolf.storage import Store


@asynccontextmanager
async def opened(app):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        yield client


@pytest_asyncio.fixture
async def client(tmp_path):
    app = create_app(tmp_path / "game.sqlite3", default_playback="auto", reading_scale=0)
    async with opened(app) as connection:
        yield connection, app


async def create(client, **kwargs):
    response = await client.post("/api/games", json={"seed": 38, **kwargs})
    assert response.status_code == 201, response.text
    data = response.json()
    return data["game"]["game_id"], data


async def ready(client, game_id):
    for _ in range(200):
        response = await client.get(f"/api/games/{game_id}")
        assert response.status_code == 200
        state = response.json()
        if state["status"] != "running" or state["turn_version"]:
            return state
        await asyncio.sleep(0.001)
    pytest.fail("API 未推进到人类回合/暂停/结束")


def submit_body(state, operation_id=None):
    return {"operation_id": operation_id or uuid4().hex, "expected_version": state["turn_version"],
            "action": asdict(simulated_action(state["view"]))}


@pytest.mark.asyncio
@pytest.mark.parametrize("human_seat", [1, 3, 4, 5])
async def test_complete_human_wolf_seer_villager_games_and_persisted_usage(client, human_seat):
    connection, app = client
    game_id, _ = await create(connection, human_seat=human_seat)
    for _ in range(100):
        state = await ready(connection, game_id)
        if state["status"] == "finished":
            break
        assert state["status"] == "running"
        result = await connection.post(f"/api/games/{game_id}/actions", json=submit_body(state))
        assert result.status_code == 200, result.text
    else:
        pytest.fail("完整 API 对局未结束")
    replay = (await connection.get(f"/api/games/{game_id}/replay")).json()
    assert len(replay["players"]) == 6
    assert replay["winner"] in {"good", "wolves"}
    assert replay["usage"]
    assert all(r["seat"] != human_seat for r in replay["usage"])
    assert all(r["simulated"] and r["status"] == "ok" for r in replay["usage"])
    assert all(r["usage"]["input_tokens"] is None for r in replay["usage"])
    assert len(replay["usage"]) == len(app.state.service.store.attempts(game_id))
    assert (await connection.post("/api/games", json={"seed": 7})).status_code == 201


@pytest.mark.asyncio
async def test_action_idempotency_and_conflicting_payload(client):
    connection, app = client
    game_id, created = await create(connection)
    body = submit_body(created["game"], "one-action")
    original = await connection.post(f"/api/games/{game_id}/actions", json=body)
    assert original.status_code == 200
    await ready(connection, game_id)
    snapshot = app.state.service.games[game_id].engine.snapshot()
    repeat = await connection.post(f"/api/games/{game_id}/actions", json=body)
    assert repeat.json() == original.json()
    assert app.state.service.games[game_id].engine.snapshot() == snapshot
    body["action"]["text"] = "不同请求"
    assert (await connection.post(f"/api/games/{game_id}/actions", json=body)).status_code == 409


@pytest.mark.asyncio
async def test_simultaneous_duplicate_submissions_are_only_applied_once(client):
    connection, app = client
    game_id, created = await create(connection)
    body = submit_body(created["game"])
    results = await asyncio.gather(*[
        connection.post(f"/api/games/{game_id}/actions", json=body) for _ in range(3)
    ])
    assert all(r.status_code == 200 for r in results)
    assert all(r.json() == results[0].json() for r in results)
    receipts = app.state.service.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0]
    assert receipts == 1


@pytest.mark.asyncio
async def test_stale_version_invalid_target_and_strict_request_do_not_mutate(client):
    connection, app = client
    game_id, created = await create(connection)
    before = app.state.service.games[game_id].engine.snapshot()
    bad = submit_body(created["game"])
    bad["expected_version"] = "stale"
    assert (await connection.post(f"/api/games/{game_id}/actions", json=bad)).status_code == 409
    bad["expected_version"] = created["game"]["turn_version"]
    bad["action"]["target"] = 1
    assert (await connection.post(f"/api/games/{game_id}/actions", json=bad)).status_code == 422
    bad["action"]["target"] = True
    assert (await connection.post(f"/api/games/{game_id}/actions", json=bad)).status_code == 422
    bad["seat"] = 2
    assert (await connection.post(f"/api/games/{game_id}/actions", json=bad)).status_code == 422
    assert app.state.service.games[game_id].engine.snapshot() == before


@pytest.mark.asyncio
async def test_pause_resume_invalidates_action_version_and_is_idempotent(client):
    connection, _ = client
    game_id, created = await create(connection)
    pause_body = {"operation_id": "pause-1"}
    result = await connection.post(f"/api/games/{game_id}/pause", json=pause_body)
    assert result.json()["status"] == "paused"
    assert result.json()["turn_version"] is None
    assert (await connection.post(f"/api/games/{game_id}/pause", json=pause_body)).json() == result.json()
    old = submit_body(created["game"])
    assert (await connection.post(f"/api/games/{game_id}/actions", json=old)).status_code == 409
    resumed = await connection.post(f"/api/games/{game_id}/resume", json={"operation_id": "resume-1"})
    assert resumed.json()["turn_version"] != created["game"]["turn_version"]
    assert (await connection.post(f"/api/games/{game_id}/actions", json=old)).status_code == 409
    assert (await connection.post(f"/api/games/{game_id}/resume", json=pause_body)).status_code == 409


@pytest.mark.asyncio
async def test_one_live_game_and_no_real_mode_or_missing_connection(client):
    connection, _ = client
    assert (await connection.post("/api/games", json={"mode": "real"})).status_code == 422
    assert (await connection.post("/api/games", json={"connections": [{
        "id": "real", "provider": "dashscope", "model": "qwen", "base_url": "https://example.invalid",
    }]})).status_code == 422
    assert (await connection.post("/api/games", json={"ai_profiles": []})).status_code == 422
    game_id, _ = await create(connection)
    assert (await connection.post("/api/games", json={})).status_code == 409
    await connection.post(f"/api/games/{game_id}/pause", json={"operation_id": "pause"})
    assert (await connection.post("/api/games", json={})).status_code == 409


@pytest.mark.asyncio
async def test_api_does_not_offer_other_seats_or_secret_snapshots(client):
    connection, _ = client
    game_id, _ = await create(connection, human_seat=4)
    state = await ready(connection, game_id)
    assert state["view"]["role"] == "villager"
    state = (await connection.get(f"/api/games/{game_id}?seat=1")).json()
    assert state["view"]["seat"] == 4
    assert state["view"]["checks"] == []
    assert state["view"]["wolf_teammates"] == []
    assert all(set(p) == {"seat", "alive"} for p in state["view"]["players"])
    assert (await connection.get(f"/api/games/{game_id}/replay")).status_code == 409
    assert (await connection.get(f"/api/games/{game_id}/snapshot")).status_code == 404
    batches = (await connection.get(f"/api/games/{game_id}/events")).json()
    for packet in batches["events"]:
        view = packet["state"]["view"]
        assert not any(e["kind"].startswith("wolf_") or e["kind"] == "inspection" for e in view["events"])
        assert not {"seed", "rng_state", "pending_kill", "revision"} & view.keys()
        assert "token_hash" not in canonical_json(packet)
    night_packets = [p for p in batches["events"] if p["state"]["view"]["phase"] == "night"]
    assert len(night_packets) == 1  # Secret substeps don't produce public timing/version updates.


@pytest.mark.asyncio
async def test_auth_cookie_bearer_and_no_read_or_write_without_session(client):
    connection, _ = client
    game_id, created = await create(connection)
    connection.cookies.clear()
    for path in ["", "/events", "/stream?follow=false", "/replay"]:
        assert (await connection.get(f"/api/games/{game_id}{path}")).status_code == 403
    assert (await connection.post(f"/api/games/{game_id}/pause", json={"operation_id": "p"})).status_code == 403
    headers = {"Authorization": f"Bearer {created['session_token']}"}
    assert (await connection.get(f"/api/games/{game_id}", headers=headers)).status_code == 200
    assert (await connection.get(f"/api/games/{game_id}", headers={"Authorization": "Bearer wrong"})).status_code == 403


@pytest.mark.asyncio
async def test_sse_resume_cursor_and_reads_never_invoke_adapter(client):
    connection, app = client
    game_id, _ = await create(connection)
    assert not app.state.service.store.attempts(game_id)
    packets = (await connection.get(f"/api/games/{game_id}/events")).json()
    cursor = packets["cursor"]
    response = await connection.get(f"/api/games/{game_id}/stream?follow=false")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {cursor}\n" in response.text and "event: state" in response.text
    resumed = await connection.get(f"/api/games/{game_id}/stream?follow=false", headers={"Last-Event-ID": str(cursor)})
    assert resumed.text == ""
    assert (await connection.get(f"/api/games/{game_id}/events?after={cursor}")).json()["events"] == []
    assert (await connection.get(f"/api/games/{game_id}/events?after=999")).status_code == 409
    assert (await connection.get(f"/api/games/{game_id}/stream?follow=false", headers={"Last-Event-ID": "bad"})).status_code == 422
    assert not app.state.service.store.attempts(game_id)


@pytest.mark.asyncio
async def test_restart_preserves_state_receipts_stream_and_rulebook(tmp_path):
    path = tmp_path / "restart.sqlite3"
    app = create_app(path, default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        game_id, created = await create(client)
        body = submit_body(created["game"], "saved-action")
        receipt = (await client.post(f"/api/games/{game_id}/actions", json=body)).json()
        await ready(client, game_id)
        service = app.state.service
        snapshot = service.games[game_id].engine.snapshot()
        rule_hash = service.games[game_id].book.fingerprint
        count = len(service.store.attempts(game_id))
        cursor = service.store.cursor(game_id)
    app = create_app(path, default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        client.headers["Authorization"] = f"Bearer {created['session_token']}"
        state = (await client.get(f"/api/games/{game_id}")).json()
        assert state["status"] == "paused" and state["pause_reason"] == "server_restarted"
        assert app.state.service.games[game_id].engine.snapshot() == snapshot
        assert app.state.service.games[game_id].book.fingerprint == rule_hash
        assert len(app.state.service.store.attempts(game_id)) == count
        assert (await client.post(f"/api/games/{game_id}/actions", json=body)).json() == receipt
        events = (await client.get(f"/api/games/{game_id}/events?after={cursor}")).json()
        assert events["events"][-1]["state"]["status"] == "paused"
        resumed = await client.post(f"/api/games/{game_id}/resume", json={"operation_id": "resume"})
        assert resumed.status_code == 200
        assert (await ready(client, game_id))["turn_version"]


@pytest.mark.asyncio
async def test_mock_failure_pauses_with_sanitized_reason_and_unknown_usage(tmp_path):
    class Failed:
        async def complete(self, request):
            raise TimeoutError("SECRET_ROLE_OR_KEY")

    app = create_app(tmp_path / "failed.sqlite3", Failed(), default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        game_id, _ = await create(client, human_seat=3)
        state = await ready(client, game_id)
        assert state["status"] == "paused" and state["pause_reason"] == "mock_decision_failed"
        assert "SECRET_ROLE_OR_KEY" not in canonical_json(state)
        records = app.state.service.store.attempts(game_id)
        assert len(records) == 1
        assert records[0]["usage"]["input_tokens"] is None
        app.state.service.adapter = MockAdapter()
        assert (await client.post(f"/api/games/{game_id}/resume", json={"operation_id": "retry"})).status_code == 200
        state = await ready(client, game_id)
        assert state["turn_version"] and state["status"] == "running"


@pytest.mark.asyncio
async def test_pausing_in_flight_discards_late_response(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Late(MockAdapter):
        async def complete(self, request):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # Emulate a transport that ignores cancellation.
            return await super().complete(request)

    app = create_app(tmp_path / "late.sqlite3", Late(), default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        game_id, _ = await create(client, human_seat=3)
        await started.wait()
        runtime = app.state.service.games[game_id]
        before = runtime.engine.snapshot()
        result = await client.post(f"/api/games/{game_id}/pause", json={"operation_id": "pause"})
        assert result.json()["status"] == "paused"
        release.set()
        for _ in range(100):
            if runtime.worker is None:
                break
            await asyncio.sleep(0.001)
        assert runtime.engine.snapshot() == before
        assert app.state.service.store.attempts(game_id)[0]["status"] == "discarded"


def test_sqlite_atomic_commit_rolls_back_all_related_changes(tmp_path):
    store = Store(tmp_path / "atomic.sqlite3")
    original = {"status": "running", "secret": "old"}
    visible = {"state": "original"}
    store.commit("game", original, visible)
    with pytest.raises(ValueError, match="事务回滚"):
        store.commit("game", {"status": "finished", "secret": "new"}, {"state": "changed"},
                     receipt=("id", "hash", {"accepted": True}), attempt=("missing", {"status": "ok"}))
    assert store.load_games() == [original]
    assert store.events("game", 0) == [{"id": 1, "state": visible}]
    assert store.receipt("game", "id") is None
    store.start_attempt("pending", "game", {"status": "in_flight", "usage": None})
    store.interrupt_attempts()
    assert store.attempts("game")[0]["status"] == "interrupted"
    store.close()


def test_database_future_version_is_rejected_without_overwrite(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="数据库版本"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


@pytest.mark.asyncio
async def test_health_openapi_have_no_startup_model_calls(client):
    connection, app = client
    assert (await connection.get("/api/health")).json() == {
        "status": "ok", "mode": "mock_and_models", "database_version": 1,
        "connected_providers": ["deepseek", "siliconflow", "dashscope"], "siliconflow_models": ["tencent/Hy4-preview", "zai-org/GLM-5.3"],
        "qualification_required": False,
        "new_game_limits": {"night_reasoning":512, "day_reasoning":1024, "night_output":512, "day_output":768,
                            "reasoning_budget_provider":"siliconflow", "thinking_mode":"requested_disabled",
                            "decision_budget_policy": {**asdict(V10_BUDGET_POLICY), "gods": list(V10_BUDGET_POLICY.gods)}},
    }
    schema = (await connection.get("/openapi.json")).json()
    assert "/api/games/{game_id}/actions" in schema["paths"]
    assert "/api/games/{game_id}/snapshot" not in schema["paths"]
    assert not app.state.service.games


@pytest.mark.asyncio
async def test_decision_timeout_pauses_without_state_change(tmp_path):
    class NeverReturns:
        async def complete(self, request):
            await asyncio.Event().wait()

    app = create_app(tmp_path / "timeout.sqlite3", NeverReturns(), decision_timeout=0.01, default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        game_id, _ = await create(client, human_seat=3)
        runtime = app.state.service.games[game_id]
        before = runtime.engine.snapshot()
        state = await ready(client, game_id)
        assert state["status"] == "paused"
        assert runtime.engine.snapshot() == before
        record = app.state.service.store.attempts(game_id)[0]
        assert record["error_type"] == "TimeoutError"
        assert record["usage"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_recovery_marks_inflight_attempt_unknown_not_zero(tmp_path):
    path = tmp_path / "interrupted.sqlite3"
    app = create_app(path, default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        game_id, created = await create(client)
        app.state.service.store.start_attempt("crashed-call", game_id, {
            "status": "in_flight", "usage": {"input_tokens": None, "output_tokens": None},
            "purpose": "game", "seat": 2,
        })
    app = create_app(path, default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        client.headers["Authorization"] = f"Bearer {created['session_token']}"
        assert (await client.get(f"/api/games/{game_id}")).json()["status"] == "paused"
        record = app.state.service.store.attempts(game_id)[0]
        assert record["status"] == "interrupted" and record["usage"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_unavailable_rulebook_blocks_creation_without_partial_game(client, monkeypatch):
    connection, app = client

    def unavailable():
        raise RulebookError("missing")

    monkeypatch.setattr("werewolf.service.Rulebook.load", unavailable)
    assert (await connection.post("/api/games", json={})).status_code == 503
    assert not app.state.service.games
    assert not app.state.service.store.load_games()


@pytest.mark.asyncio
async def test_service_db_failure_does_not_commit_human_action_or_receipt(client, monkeypatch):
    connection, app = client
    game_id, created = await create(connection)
    runtime = app.state.service.games[game_id]
    before = runtime.engine.snapshot()
    stored_before = app.state.service.store.load_games()
    original_commit = app.state.service.store.commit

    def failing(*args, **kwargs):
        raise sqlite3.OperationalError("temporary failure")

    monkeypatch.setattr(app.state.service.store, "commit", failing)
    body = submit_body(created["game"])
    with pytest.raises(sqlite3.OperationalError):
        await connection.post(f"/api/games/{game_id}/actions", json=body)
    assert runtime.engine.snapshot() == before
    assert app.state.service.store.load_games() == stored_before
    assert app.state.service.store.receipt(game_id, body["operation_id"]) is None
    monkeypatch.setattr(app.state.service.store, "commit", original_commit)
