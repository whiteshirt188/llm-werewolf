"""Variable rosters, shared API isolation and per-roster admission without network."""

import json
from collections import Counter
from dataclasses import asdict

import pytest
from app_helpers import flow_app as create_app
from pydantic import ValidationError
from test_api import opened, ready, submit_body
from test_qualification import environment, finish
from test_real_games import GameDouble

from werewolf.api import CreateGame
from werewolf.demo import run_demo, simulated_action
from werewolf.engine import GameConfig, GameEngine, RuleViolation
from werewolf.profiles import PersonaProfile
from werewolf.qualification_questions import questions
from werewolf.roster import RosterConfig
from werewolf.table_plan import TablePlan

NINE = {"players": 9, "wolves": 3, "seers": 1, "witches": 1, "hunters": 1}


@pytest.mark.parametrize("values", [NINE, {"players": 7, "wolves": 2, "seers": 1, "witches": 1, "guards": 1},
                                  {"players": 8, "wolves": 1, "seers": 1, "witches": 0, "hunters": 1, "knights": 1},
                                  {"players": 9, "wolves": 4, "seers": 1, "witches": 0, "guards": 1, "knights": 1}])
def test_rosters_complete_reproducibly_restore_and_only_supply_private_permissions(values):
    config = RosterConfig(**values).engine_config()
    for seed in range(12):
        game = run_demo(seed, config)
        assert game.replay() == run_demo(seed, config).replay()
        assert Counter(p["role"] for p in game.replay()["players"]) == Counter({
            "wolf": config.wolves, "seer": config.seers, "witch": config.witches, "villager": config.villagers,
            "hunter": config.hunters, "guard": config.guards, "knight": config.knights})
        running = GameEngine(seed, config)
        for _ in range(200):
            for seat in range(1, config.players + 1):
                view = running.player_view(seat)
                assert len(view["players"]) == config.players
                assert view["rules"] == asdict(config)
                if not running.finished:
                    assert all("role" not in p for p in view["players"])
                if view["role"] != "wolf":
                    assert not view["wolf_teammates"]
                if view["role"] != "witch":
                    assert "witch" not in view
            if running.finished:
                break
            restored = GameEngine.from_snapshot(json.loads(json.dumps(running.snapshot())))
            assert restored.snapshot() == running.snapshot()
            actor = running.next_actor()
            action = simulated_action(running.player_view(actor))
            restored.submit(actor, action)
            running.submit(actor, action)
            assert restored.snapshot() == running.snapshot()
        assert running.finished


@pytest.mark.parametrize("values", [{"players": 5}, {"players": 10}, {"wolves": 0}, {"wolves": 3},
                                  {"seers": 2}, {"witches": 2}, {"players": True}, {"wolves": 1.5},
                                  {"players": 9, "wolves": 5}, {"villagers": 3}, {"speech_limit": 500}])
def test_invalid_rosters_rejected(values):
    with pytest.raises(ValidationError):
        RosterConfig(**values)


def test_presets_legacy_and_raw_engine_configuration_are_checked():
    assert RosterConfig().engine_config() == GameConfig(last_words=True)
    assert RosterConfig(**NINE).engine_config().version == "nine-player-v2-gods"
    assert GameEngine.from_snapshot(GameEngine(38, GameConfig.legacy()).snapshot()).config == GameConfig.legacy()
    with pytest.raises(RuleViolation):
        GameConfig(version="custom-player-v1", players=9, wolves=3, villagers=5)
    with pytest.raises(RuleViolation):
        GameConfig(version="custom-player-v1", players=8, wolves=4, villagers=2)
    assert len(CreateGame(roster=RosterConfig(**NINE), human_seat=9).ai_profiles) == 8
    with pytest.raises(ValidationError):
        CreateGame(human_seat=9)


@pytest.mark.asyncio
@pytest.mark.parametrize("human", [1, 7, 9])
async def test_nine_player_api_game_and_persistence(tmp_path, human):
    path = tmp_path / "game.sqlite3"
    app = create_app(path, reading_scale=0, default_playback="auto")
    async with opened(app) as client:
        created = await client.post("/api/games", json={"roster": NINE, "human_seat": human, "seed": 38})
        assert created.status_code == 201, created.text
        gid = created.json()["game"]["game_id"]
        assert len(created.json()["game"]["seat_profiles"]) == 8
        for _ in range(150):
            state = await ready(client, gid)
            if state["status"] == "finished":
                break
            result = await client.post(f"/api/games/{gid}/actions", json=submit_body(state))
            assert result.status_code == 200, result.text
        assert state["status"] == "finished"
        replay = (await client.get(f"/api/games/{gid}/replay")).json()
        assert len(replay["players"]) == 9 and all(r["seat"] != human for r in replay["usage"])
    restarted = create_app(path, reading_scale=0)
    async with opened(restarted):
        runtime = restarted.state.service.games[gid]
        assert runtime.engine.config.players == 9 and runtime.engine.replay()["winner"] == replay["winner"]


@pytest.mark.asyncio
async def test_roster_qualification_shared_by_eight_seats_but_never_by_other_roster(tmp_path):
    game = GameDouble()
    async with environment(tmp_path, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, exam):
        base = {"connection_id": cid}
        six = (await client.post("/api/account/qualifications/preview", headers=headers, json=base)).json()
        nine = (await client.post("/api/account/qualifications/preview", headers=headers, json={**base, "roster": NINE})).json()
        assert six["configuration_version"] != nine["configuration_version"] and not exam.calls
        started = await client.post("/api/account/qualifications", headers=headers, json={
            **base, "roster": NINE, "operation_id": "nine-exam", "expected_version": nine["configuration_version"], "confirm_cost": True})
        assert started.status_code == 202, started.text
        passed = await finish(app, client, started.json())
        assert passed["qualified"] and len(exam.calls) == 13
        assert all(json.loads(r.messages[-1].content)["state"]["rules"]["players"] == 9 for r in exam.calls)
        table = {"roster": NINE, "human_seat": 9, "seed": 38, "playback_mode": "auto", "ai_profiles": [
            {"seat": s, "connection_id": cid, "persona": PersonaProfile().model_dump()} for s in range(1, 9)]}
        checked = await client.post("/api/account/table-plan/admission", headers=headers, json=table)
        assert checked.status_code == 200, checked.text
        admission = checked.json()
        assert admission["all_qualified"] and len(admission["seats"]) == 8
        assert admission["qualification_configurations"] == 1 and len(exam.calls) == 13
        assert all(s["report_id"] == passed["id"] for s in admission["seats"])
        old = (await client.post("/api/account/qualifications/preview", headers=headers, json=base)).json()
        assert not old["cached"]
        result = await client.post("/api/account/games", headers=headers, json={
            "plan": table, "operation_id": "nine-start", "confirm_cost": True,
            "expected_versions": {str(s["seat"]): s["configuration_version"] for s in admission["seats"]}})
        assert result.status_code == 201, result.text
        gid = result.json()["game"]["game_id"]
        for _ in range(150):
            state = await ready(client, gid)
            if state["status"] == "finished":
                break
            assert state["status"] == "running"
            assert (await client.post(f"/api/games/{gid}/actions", json=submit_body(state))).status_code == 200
        assert state["status"] == "finished"
        assert game.calls and all(json.loads(r.messages[-1].content)["state"]["rules"]["players"] == 9 for r in game.calls)
        assert all(r.connection.id == cid and r.connection.model == "fixed-chosen-model" for r in game.calls)
        assert len(exam.calls) == 13


def test_plan_rejects_missing_duplicate_or_out_of_range_seats():
    profiles = [{"seat": s, "connection_id": "shared"} for s in range(2, 10)]
    assert len(TablePlan(roster=NINE, ai_profiles=profiles).ai_profiles) == 8
    for bad in (profiles[:-1], profiles + [profiles[0]], [*profiles[:-1], {"seat": 10, "connection_id": "shared"}]):
        with pytest.raises(ValidationError):
            TablePlan(roster=NINE, ai_profiles=bad)


def test_no_absent_role_or_private_skill_in_custom_exam_contexts():
    # Historical custom saves without a seer remain readable; new inputs require one.
    rules = asdict(GameConfig(version="custom-player-v1", players=9, wolves=1, seers=0, witches=0, villagers=8))
    for q in questions(rules):
        assert len(q.view["players"]) == 9 and q.view["rules"] == rules
        assert q.view["role"] in {"wolf", "villager"}
        assert not q.view["checks"] and not q.view["wolf_teammates"] and "witch" not in q.view
