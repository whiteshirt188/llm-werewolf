import asyncio

import pytest
from app_helpers import flow_app as create_app
from test_api import opened, submit_body

from werewolf.mock_adapter import MockAdapter
from werewolf.profiles import content_hash


async def prepared(app, game_id):
    async with asyncio.timeout(2):
        while app.state.service.games[game_id].prepared is None:
            await asyncio.sleep(0.001)


async def advance(client, state, operation_id):
    return await client.post(f"/api/games/{state['game_id']}/advance", json={
        "operation_id": operation_id, "expected_step": state["step_id"],
    })


@pytest.mark.asyncio
async def test_default_manual_prefetch_is_one_hidden_unsettled_action_and_reads_do_not_call(tmp_path):
    app = create_app(tmp_path / "manual.sqlite3", reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        state = created["game"]
        game_id = state["game_id"]
        runtime = app.state.service.games[game_id]
        before = content_hash(runtime.engine.snapshot())
        assert state["playback_mode"] == "manual" and state["step_waiting"]
        await prepared(app, game_id)
        assert content_hash(runtime.engine.snapshot()) == before
        assert len(app.state.service.store.attempts(game_id)) == 1
        assert app.state.service.store.attempts(game_id)[0]["status"] == "ok"
        for _ in range(4):
            visible = (await client.get(f"/api/games/{game_id}")).json()
            assert visible == state
            packets = (await client.get(f"/api/games/{game_id}/events")).json()
            assert "prepared" not in str(packets)
            assert all(event["kind"] in {"game_started", "night_started"} for event in visible["view"]["events"])
            await asyncio.sleep(0.001)
        assert len(app.state.service.store.attempts(game_id)) == 1
        results = await asyncio.gather(advance(client, state, "same"), advance(client, state, "same"))
        assert all(response.status_code == 200 for response in results)
        assert results[0].json() == results[1].json()
        assert (await advance(client, state, "different")).status_code == 409
        # Hidden night flow may continue, but it stops at dawn and never previews speech.
        async with asyncio.timeout(2):
            while runtime.engine.player_view(4)["phase"] == "night":
                await asyncio.sleep(0.001)
        await prepared(app, game_id)
        visible = (await client.get(f"/api/games/{game_id}")).json()
        assert visible["step_waiting"] and visible["step_id"] != state["step_id"]
        assert not any(event["kind"] == "speak" for event in visible["view"]["events"])
        count = len(app.state.service.store.attempts(game_id))
        await asyncio.sleep(0.02)
        assert len(app.state.service.store.attempts(game_id)) == count


@pytest.mark.asyncio
async def test_prepared_action_survives_pause_restart_and_is_not_recalled(tmp_path):
    path = tmp_path / "restart.sqlite3"
    app = create_app(path, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        game_id, token = created["game"]["game_id"], created["session_token"]
        await prepared(app, game_id)
        buffered = app.state.service.games[game_id].prepared
        await client.post(f"/api/games/{game_id}/pause", json={"operation_id": "pause"})
        assert (await advance(client, created["game"], "paused-next")).status_code == 409
    restarted = create_app(path, reading_scale=0)
    async with opened(restarted) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        runtime = restarted.state.service.games[game_id]
        assert runtime.prepared == buffered
        assert len(restarted.state.service.store.attempts(game_id)) == 1
        await client.post(f"/api/games/{game_id}/resume", json={"operation_id": "resume"})
        await asyncio.sleep(0.01)
        assert len(restarted.state.service.store.attempts(game_id)) == 1
        assert runtime.prepared == buffered
        state = (await client.get(f"/api/games/{game_id}")).json()
        assert (await advance(client, state, "next")).status_code == 200
        async with asyncio.timeout(2):
            while runtime.engine.next_actor() == 1 and runtime.engine.player_view(4)["phase"] == "night":
                await asyncio.sleep(0.001)
        records = restarted.state.service.store.attempts(game_id)
        assert sum(record["seat"] == 1 and record["action_kind"] == "propose_kill" for record in records) == 1


@pytest.mark.asyncio
async def test_full_pause_cancels_running_prefetch_without_publishing(tmp_path):
    class Blocked(MockAdapter):
        def __init__(self):
            self.started = asyncio.Event()

        async def complete(self, request):
            self.started.set()
            await asyncio.Event().wait()

    adapter = Blocked()
    app = create_app(tmp_path / "cancel.sqlite3", adapter, reading_scale=0)
    async with opened(app) as client:
        state = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()["game"]
        game_id = state["game_id"]
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        runtime = app.state.service.games[game_id]
        before = runtime.engine.snapshot()
        await client.post(f"/api/games/{game_id}/pause", json={"operation_id": "pause"})
        await asyncio.sleep(0.01)
        assert runtime.engine.snapshot() == before and runtime.prepared is None
        records = app.state.service.store.attempts(game_id)
        assert len(records) == 1 and records[0]["status"] == "cancelled"
        assert records[0]["usage"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_mode_change_while_prefetch_in_flight_does_not_duplicate_or_discard_valid_action(tmp_path):
    class Controlled(MockAdapter):
        def __init__(self):
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def complete(self, request):
            self.started.set()
            await self.release.wait()
            return await super().complete(request)

    adapter = Controlled()
    app = create_app(tmp_path / "switch.sqlite3", adapter, reading_scale=0.04)
    async with opened(app) as client:
        state = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()["game"]
        game_id = state["game_id"]
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        before = app.state.service.games[game_id].engine.snapshot()
        response = await client.post(f"/api/games/{game_id}/playback", json={"operation_id": "auto", "mode": "auto"})
        assert response.json()["playback_mode"] == "auto" and not response.json()["step_waiting"]
        adapter.release.set()
        await asyncio.sleep(0.01)
        runtime = app.state.service.games[game_id]
        assert runtime.engine.snapshot() == before
        assert runtime.prepared is not None
        async with asyncio.timeout(4):
            runtime = app.state.service.games[game_id]
            while runtime.engine.next_actor() != 4 or runtime.reading_remaining:
                await asyncio.sleep(0.001)
        records = app.state.service.store.attempts(game_id)
        assert all(record["status"] == "ok" for record in records)
        assert sum(record["seat"] == 1 and record["action_kind"] == "propose_kill" for record in records) == 1
        switched = await client.post(f"/api/games/{game_id}/playback", json={"operation_id": "manual", "mode": "manual"})
        assert switched.json()["playback_mode"] == "manual"
        assert (await client.post(f"/api/games/{game_id}/playback", json={"operation_id": "bad", "mode": "invalid"})).status_code == 422


@pytest.mark.asyncio
async def test_vote_intro_waits_at_final_speech_and_clears_on_continue(tmp_path):
    app = create_app(tmp_path / "vote-intro.sqlite3", reading_scale=0)
    async with opened(app) as client:
        state = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()["game"]
        game_id = state["game_id"]
        for index in range(200):
            state = (await client.get(f"/api/games/{game_id}")).json()
            if state["speech_to_vote"]:
                break
            if state["step_waiting"]:
                assert (await advance(client, state, f"step-{index}")).status_code == 200
            elif state["turn_version"]:
                assert (await client.post(f"/api/games/{game_id}/actions", json=submit_body(state))).status_code == 200
            else:
                await asyncio.sleep(0.001)
        else:
            pytest.fail("final speech gate was not reached")
        assert state["view"]["phase"] == "vote" and state["step_waiting"]
        assert not state["view"]["legal_actions"] and not state["turn_version"]
        await prepared(app, game_id)
        held = (await client.get(f"/api/games/{game_id}")).json()
        assert held["speech_to_vote"] and held["step_id"] == state["step_id"]
        released = (await advance(client, held, "enter-voting")).json()
        assert not released["speech_to_vote"]
        async with asyncio.timeout(2):
            while app.state.service.games[game_id].engine.next_actor() != 4:
                await asyncio.sleep(0.001)
        # Changing playback mode during an existing vote must not replay its intro.
        for mode in ["auto", "manual"]:
            changed = (await client.post(f"/api/games/{game_id}/playback", json={
                "operation_id": f"mode-{mode}", "mode": mode,
            })).json()
            assert changed["view"]["phase"] == "vote" and not changed["speech_to_vote"]


@pytest.mark.asyncio
@pytest.mark.parametrize("human_seat", [1, 3, 4, 5])
@pytest.mark.parametrize("seed", [38, 1, 2])
async def test_manual_complete_games_match_auto_results_and_calls(tmp_path, human_seat, seed):
    outcomes = []
    for mode in ["auto", "manual"]:
        app = create_app(tmp_path / f"full-{mode}.sqlite3", reading_scale=0)
        async with opened(app) as client:
            state = (await client.post("/api/games", json={"seed": seed, "human_seat": human_seat, "playback_mode": mode})).json()["game"]
            game_id = state["game_id"]
            for index in range(1000):
                state = (await client.get(f"/api/games/{game_id}")).json()
                if state["status"] == "finished":
                    break
                if state["step_waiting"]:
                    assert (await advance(client, state, f"step-{index}")).status_code == 200
                elif state["turn_version"]:
                    assert (await client.post(f"/api/games/{game_id}/actions", json=submit_body(state))).status_code == 200
                else:
                    await asyncio.sleep(0.001)
            else:
                pytest.fail("manual game did not finish")
            replay = (await client.get(f"/api/games/{game_id}/replay")).json()
            if seed in {1, 2}:
                assert any(event["kind"] == "runoff_started" for event in replay["events"])
                assert any(event["kind"] == "vote_result" and event["data"]["round"] == 2 for event in replay["events"])
            outcomes.append((replay["winner"], replay["events"], len(replay["usage"])))
    assert outcomes[0] == outcomes[1]
