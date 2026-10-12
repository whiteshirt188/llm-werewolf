import asyncio
from time import monotonic

import pytest
from app_helpers import flow_app as create_app
from test_api import opened, submit_body

from werewolf.engine import Action


async def ready(client, game_id):
    async with asyncio.timeout(4):
        while True:
            state = (await client.get(f"/api/games/{game_id}")).json()
            if state["status"] != "running" or state["turn_version"]:
                return state
            await asyncio.sleep(0.001)


async def reading(client, game_id):
    for _ in range(1000):
        state = (await client.get(f"/api/games/{game_id}")).json()
        if state["reading"]:
            return state
        await asyncio.sleep(0.001)
    pytest.fail("did not enter a reading window")


@pytest.mark.asyncio
async def test_next_agent_does_not_start_during_reading_and_pause_creates_no_attempt(tmp_path):
    app = create_app(tmp_path / "paced.sqlite3", default_playback="auto", reading_scale=0.04)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        game_id = created["game"]["game_id"]
        state = await reading(client, game_id)
        runtime = app.state.service.games[game_id]
        assert state["turn_version"] is None
        assert state["view"]["legal_actions"] == []
        assert state["view"].get("current_actor") is None
        count = len(app.state.service.store.attempts(game_id))
        await asyncio.sleep(0.03)
        assert len(app.state.service.store.attempts(game_id)) == count
        await client.post(f"/api/games/{game_id}/pause", json={"operation_id": "pause"})
        await asyncio.sleep(0.01)
        remaining = runtime.reading_left()
        await asyncio.sleep(0.03)
        assert runtime.reading_left() == remaining
        assert len(app.state.service.store.attempts(game_id)) == count
        assert all(item["status"] == "ok" for item in app.state.service.store.attempts(game_id))
        await client.post(f"/api/games/{game_id}/resume", json={"operation_id": "resume"})
        await asyncio.sleep(0.01)
        assert len(app.state.service.store.attempts(game_id)) == count
        state = await ready(client, game_id)
        assert state["turn_version"]
        assert len(app.state.service.store.attempts(game_id)) > count


@pytest.mark.asyncio
async def test_human_cannot_bypass_reading_gate_and_refresh_does_not_schedule(tmp_path):
    app = create_app(tmp_path / "gate.sqlite3", default_playback="auto", reading_scale=0.01)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        game_id = created["game"]["game_id"]
        runtime = app.state.service.games[game_id]
        for _ in range(1000):
            if runtime.reading_remaining and runtime.engine.next_actor() == 4:
                break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("human gate not reached")
        before = runtime.engine.snapshot()
        count = len(app.state.service.store.attempts(game_id))
        rejected = await client.post(f"/api/games/{game_id}/actions", json={
            "operation_id": "bypass", "expected_version": runtime.turn_version,
            "action": {"kind": "speak", "text": "不能跳过上一位"},
        })
        assert rejected.status_code == 409
        assert runtime.engine.snapshot() == before
        for _ in range(3):
            await client.get(f"/api/games/{game_id}")
            await client.get(f"/api/games/{game_id}/events")
        assert len(app.state.service.store.attempts(game_id)) == count
        state = await ready(client, game_id)
        assert not state["reading"]
        assert (await client.post(f"/api/games/{game_id}/actions", json=submit_body(state))).status_code == 200


@pytest.mark.asyncio
async def test_restart_preserves_remaining_wait_and_no_ghost_call(tmp_path):
    path = tmp_path / "restart.sqlite3"
    app = create_app(path, default_playback="auto", reading_scale=0.04)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        game_id, token = created["game"]["game_id"], created["session_token"]
        await reading(client, game_id)
        count = len(app.state.service.store.attempts(game_id))
    restarted = create_app(path, default_playback="auto", reading_scale=0.04)
    async with opened(restarted) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        runtime = restarted.state.service.games[game_id]
        state = (await client.get(f"/api/games/{game_id}")).json()
        assert state["status"] == "paused" and state["reading"]
        remaining = runtime.reading_left()
        await asyncio.sleep(0.02)
        assert runtime.reading_left() == remaining
        assert len(restarted.state.service.store.attempts(game_id)) == count
        await client.post(f"/api/games/{game_id}/resume", json={"operation_id": "resume"})
        await asyncio.sleep(0.01)
        assert len(restarted.state.service.store.attempts(game_id)) == count
        assert runtime.reading_deadline > monotonic()


@pytest.mark.asyncio
async def test_private_text_only_affects_authorized_living_wolf_reading(tmp_path):
    app = create_app(tmp_path / "privacy.sqlite3")
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 3})).json()
        runtime = app.state.service.games[created["game"]["game_id"]]
        events = [{"audience": [1, 2], "kind": "wolf_replies", "data": {"replies": {"1": "秘密" * 50}}}]
        assert app.state.service._reading_delay(runtime, runtime.engine, events) == 0
        public = [{"audience": None, "kind": "speak", "data": {"text": "🐺" * 10}}]
        assert app.state.service._reading_delay(runtime, runtime.engine, public) == pytest.approx(5.6)
        runtime.human_seat = 1
        assert app.state.service._reading_delay(runtime, runtime.engine, events) == 0  # own speech instant
        events[0]["data"]["replies"] = {"2": "秘密"}
        assert app.state.service._reading_delay(runtime, runtime.engine, events) > 0
        # Trusted engine setup: eliminate seat 1, then private batches must add no spectator wait.
        engine = runtime.engine
        engine.submit(1, Action("propose_kill", target=3, text="a"))
        engine.submit(2, Action("propose_kill", target=3, text="b"))
        engine.submit(3, Action("inspect", target=1))
        engine.submit(5, Action("witch", potion="skip"))
        engine.submit(3, Action("last_words", text=""))
        for seat in [1, 2, 4, 5, 6]:
            engine.submit(seat, Action("speak", text="a"))
        for seat in [1, 2, 4, 5, 6]:
            engine.submit(seat, Action("vote", target=1 if seat != 1 else 2))
        assert not engine.player_view(1)["alive"]
        assert app.state.service._reading_delay(runtime, engine, events) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("human_seat", [1, 3, 4])
async def test_pacing_does_not_change_complete_game_or_call_count(tmp_path, human_seat):
    results = []
    for scale in [0, 0.001]:
        app = create_app(tmp_path / f"full-{scale}.sqlite3", default_playback="auto", reading_scale=scale)
        async with opened(app) as client:
            created = (await client.post("/api/games", json={"seed": 38, "human_seat": human_seat})).json()
            game_id = created["game"]["game_id"]
            for _ in range(100):
                state = await ready(client, game_id)
                if state["status"] == "finished":
                    break
                assert state["status"] == "running"
                result = await client.post(f"/api/games/{game_id}/actions", json=submit_body(state))
                assert result.status_code == 200
            else:
                pytest.fail("game did not finish")
            replay = (await client.get(f"/api/games/{game_id}/replay")).json()
            results.append((replay["winner"], replay["events"], len(replay["usage"])))
    assert results[0] == results[1]
