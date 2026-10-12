import httpx
import pytest
from app_helpers import flow_app as create_app

from werewolf.engine import Action, GameEngine
from werewolf.profiles import content_hash


@pytest.mark.asyncio
async def test_web_static_assets_do_not_shadow_api_or_expose_workspace(tmp_path):
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text('<html lang="zh-CN"><body>夜谈</body></html>', encoding="utf-8")
    (assets / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    app = create_app(tmp_path / "game.sqlite3", web_path=assets, default_playback="auto", reading_scale=0)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert "夜谈" in response.text
        assert (await client.get("/favicon.svg")).status_code == 200
        assert (await client.get("/api/health")).json()["status"] == "ok"
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/data/game.sqlite3")).status_code == 404
        assert (await client.get("/backend/werewolf/service.py")).status_code == 404


@pytest.mark.asyncio
async def test_recovery_cookie_and_safe_seat_metadata_do_not_schedule_agents(tmp_path):
    app = create_app(tmp_path / "game.sqlite3", default_playback="auto", reading_scale=0)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post("/api/games", json={"seed": 38, "human_seat": 1})
        data = response.json()
        game_id, token = data["game"]["game_id"], data["session_token"]
        assert len(data["game"]["seat_profiles"]) == 5
        for profile in data["game"]["seat_profiles"]:
            assert set(profile) == {"seat", "nickname", "persona", "connection_id", "model"}
        before = app.state.service.store.attempts(game_id)
        client.cookies.clear()
        assert (await client.get(f"/api/games/{game_id}")).status_code == 403
        bad = await client.post(f"/api/games/{game_id}/session", json={"token": "wrong"})
        assert bad.status_code == 403
        assert not client.cookies
        restored = await client.post(f"/api/games/{game_id}/session", json={"token": token})
        assert restored.status_code == 200
        assert "HttpOnly" in restored.headers["set-cookie"]
        assert "SameSite=strict" in restored.headers["set-cookie"]
        assert (await client.get(f"/api/games/{game_id}")).status_code == 200
        assert app.state.service.store.attempts(game_id) == before


@pytest.mark.asyncio
async def test_unbuilt_web_keeps_api_available(tmp_path):
    app = create_app(tmp_path / "game.sqlite3", web_path=tmp_path / "not-built", default_playback="auto", reading_scale=0)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/api/health")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("human_seat", [1, 3])
async def test_dead_human_api_views_are_public_until_full_replay(tmp_path, human_seat):
    app = create_app(tmp_path / "game.sqlite3", default_playback="auto", reading_scale=0)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": human_seat})).json()
        game_id = created["game"]["game_id"]
        await client.post(f"/api/games/{game_id}/pause", json={"operation_id": "pause"})
        runtime = app.state.service.games[game_id]
        engine = GameEngine(38)
        while engine.player_view(1)["phase"] == "night":
            seat = engine.next_actor()
            spec = engine.legal_actions(seat)[0]
            if spec["kind"] == "witch":
                engine.submit(seat, Action("witch", potion="skip"))
                continue
            engine.submit(seat, Action(spec["kind"], target=1 if spec["kind"] == "inspect" else
                                       3 if human_seat == 3 else 6,
                                       text="狼队私密提案" if spec["kind"] == "propose_kill" else None))
        if human_seat == 1:
            while engine.player_view(1)["phase"] == "day_speech":
                engine.submit(engine.next_actor(), Action("speak", text="公开讨论"))
            while engine.player_view(1)["phase"] == "vote":
                seat = engine.next_actor()
                engine.submit(seat, Action("vote", target=2 if seat == 1 else 1))
            # The surviving wolf can still publish private decisions on the next night.
            engine.submit(2, Action("kill", target=5))
        assert not engine.player_view(human_seat)["alive"]
        assert not engine.finished
        assert any(e["kind"] in {"inspection", "wolf_proposals", "wolf_decision"}
                   for e in engine.player_view(human_seat)["events"])
        async with runtime.lock:
            app.state.service._commit(runtime, engine, status="paused", pause_reason="user_paused")
        state = (await client.get(f"/api/games/{game_id}")).json()
        public = state["view"]
        assert public["checks"] == public["wolf_teammates"] == public["legal_actions"] == []
        assert state["turn_version"] is None
        assert all(not e["kind"].startswith("wolf_") and e["kind"] != "inspection" for e in public["events"])
        assert [e["id"] for e in public["events"]] == list(range(1, len(public["events"]) + 1))
        packets = (await client.get(f"/api/games/{game_id}/events")).json()["events"]
        assert packets[-1]["state"]["view"] == public
        stream = await client.get(f"/api/games/{game_id}/stream", params={"after": packets[-2]["id"], "follow": False})
        assert "狼队私密提案" not in stream.text
        restored = await client.post(f"/api/games/{game_id}/session", json={"token": created["session_token"]})
        assert restored.json()["view"] == public
        assert (await client.get(f"/api/games/{game_id}/replay")).status_code == 409
        # Simulate a pre-UX-1 spectator packet/receipt without overwriting any user database.
        legacy = {**state, "view": engine.player_view(human_seat)}
        assert legacy["view"] != public
        assert runtime.filter_legacy_spectator(legacy)["view"] == public
        assert legacy["view"] == engine.player_view(human_seat)
        app.state.service.store.commit(game_id, runtime.snapshot(), legacy,
                                       ("legacy-pause", content_hash({"operation": "pause"}), legacy))
        batch = (await client.get(f"/api/games/{game_id}/events", params={"after": packets[-1]["id"]})).json()
        assert batch["events"][0]["state"]["view"] == public
        repeated = await client.post(f"/api/games/{game_id}/pause", json={"operation_id": "legacy-pause"})
        assert repeated.json()["view"] == public
        assert runtime.filter_legacy_spectator({**legacy, "status": "finished"})["view"] != public
