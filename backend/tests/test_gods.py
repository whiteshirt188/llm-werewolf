"""New gods: real permission windows, simultaneous settlement and deterministic restore."""

import json
from dataclasses import asdict
from itertools import combinations

import pytest
from app_helpers import flow_app as create_app
from pydantic import ValidationError
from test_api import opened, ready, submit_body

from werewolf.demo import run_demo, simulated_action
from werewolf.engine import Action, GameConfig, GameEngine, RuleViolation
from werewolf.mock_adapter import MockAdapter
from werewolf.profiles import ModelConnection, SeatProfile
from werewolf.prompts import PromptBuilder
from werewolf.qualification_questions import questions
from werewolf.roster import RosterConfig
from werewolf.rulebook import Rulebook


def configured(*extras):
    return RosterConfig(players=9, wolves=3, witches=int("witch" in extras),
                        hunters=int("hunter" in extras), guards=int("guard" in extras),
                        knights=int("knight" in extras), last_words=False).engine_config()


def seat_of(game, role):
    return next(p["seat"] for p in game.snapshot()["state"]["players"] if p["role"] == role)


def night(game, victim, *, protect=None, potion="skip", poison=None):
    while game.player_view(1)["phase"] == "night":
        seat = game.next_actor()
        spec = game.legal_actions(seat)[0]
        kind = spec["kind"]
        if kind == "protect":
            action = Action(kind, target=protect)
        elif kind in {"kill", "propose_kill"}:
            action = Action(kind, target=victim, text="统一刀口" if kind == "propose_kill" else None)
        elif kind == "witch":
            action = Action(kind, potion=potion, target=victim if potion == "save" else poison if potion == "poison" else None)
        else:
            action = simulated_action(game.player_view(seat))
        game.submit(seat, action)


@pytest.mark.parametrize("extras", list(combinations(("witch", "hunter", "guard", "knight"), 2)))
def test_every_three_god_combination_runs_and_restores_without_secret_leaks(extras):
    config = configured(*extras)
    for seed in range(15):
        assert run_demo(seed, config).replay() == run_demo(seed, config).replay()
        game = GameEngine(seed, config)
        while not game.finished:
            for s in range(1, 10):
                view = game.player_view(s)
                assert all("role" not in p for p in view["players"])
                assert "protected" not in view and "knight_used" not in view
                assert ("guard" in view) == (view["role"] == "guard")
                assert ("knight" in view) == (view["role"] == "knight")
            restored = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
            seat = game.next_actor()
            action = simulated_action(game.player_view(seat))
            for engine in (game, restored):
                engine.submit(seat, action)
            assert game.snapshot() == restored.snapshot()


def test_hunter_death_window_is_public_legal_and_resolves_before_parity():
    game = GameEngine(38, configured("witch", "hunter"))
    hunter = seat_of(game, "hunter")
    night(game, hunter)
    assert not game.player_view(hunter)["alive"] and not game.finished
    assert game.next_actor() == hunter and game.legal_actions(hunter)[0]["kind"] == "shoot"
    # Trusted fixture models a parity position reached at dawn; must finish shot first.
    remaining_good = [s for s in game._living() if game._player(s)["role"] != "wolf"]
    for s in remaining_good[3:]:
        game._player(s)["alive"] = False
    wolf = game._living("wolf")[0]
    assert len(game._living()) == 6
    before = game.snapshot()
    for action in (Action("shoot", target=hunter), Action("shoot", target=wolf, text="理由")):
        with pytest.raises(RuleViolation):
            game.submit(hunter, action)
        assert game.snapshot() == before
    game.submit(hunter, Action("shoot", target=wolf))
    assert not game.finished and not game.player_view(wolf)["alive"]
    assert not game.legal_actions(hunter)


@pytest.mark.parametrize("also_knife", [False, True])
def test_poisoned_hunter_never_gets_shot_even_with_knife(also_knife):
    game = GameEngine(38, configured("witch", "hunter"))
    hunter = seat_of(game, "hunter")
    victim = hunter if also_knife else seat_of(game, "villager")
    night(game, victim, potion="poison", poison=hunter)
    assert not game.player_view(hunter)["alive"] and not game.legal_actions(hunter)
    assert not any(e["kind"] == "hunter_ready" for e in game.player_view(1)["events"])


def test_banished_hunter_shots_last_wolf_before_victory_and_restores_window():
    game = GameEngine(38, configured("witch", "hunter"))
    hunter = seat_of(game, "hunter")
    night(game,seat_of(game,"villager"),potion="save")
    while game.player_view(1)["phase"] == "day_speech":
        game.submit(game.next_actor(),Action("speak",text="讨论"))
    # Last-wolf fixture validates shot settlement precedence without relying on RNG.
    for wolf in game._living("wolf")[1:]:
        game._player(wolf)["alive"] = False
    while game.player_view(1)["phase"] == "vote":
        s = game.next_actor()
        if not game._player(s)["alive"]:
            game._state["cursor"] += 1
            continue
        game.submit(s,Action("vote",target=hunter if s != hunter else None))
    assert not game.finished and game.next_actor() == hunter
    clone = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
    for engine in (game,clone):
        engine.submit(hunter,Action("shoot",target=engine._living("wolf")[0]))
        assert engine.finished and engine.replay()["winner"] == "good"
    assert game.snapshot() == clone.snapshot()


def test_guard_self_protection_repeat_rejection_and_empty_night_reset():
    game = GameEngine(38, configured("witch", "guard"))
    guard = seat_of(game, "guard")
    night(game, guard, protect=guard)
    assert game.player_view(guard)["alive"]
    while game.player_view(1)["phase"] != "night":
        seat = game.next_actor()
        kind = game.legal_actions(seat)[0]["kind"]
        game.submit(seat, Action(kind, text="讨论" if kind == "speak" else None))
    assert guard not in game.legal_actions(guard)[0]["targets"]
    before = game.snapshot()
    with pytest.raises(RuleViolation):
        game.submit(guard, Action("protect", target=guard))
    assert game.snapshot() == before
    night(game, seat_of(game, "villager"), protect=None, potion="save")
    while game.player_view(1)["phase"] != "night":
        seat = game.next_actor()
        kind = game.legal_actions(seat)[0]["kind"]
        game.submit(seat, Action(kind, text="讨论" if kind == "speak" else None))
    assert guard in game.legal_actions(guard)[0]["targets"]


@pytest.mark.parametrize("potion,alive", [("save", True), ("poison", False)])
def test_guard_and_save_stack_but_poison_bypasses(potion, alive):
    game = GameEngine(38, configured("witch", "guard"))
    victim = seat_of(game, "villager")
    night(game, victim, protect=victim, potion=potion, poison=victim)
    assert game.player_view(victim)["alive"] is alive
    for s in range(1, 10):
        if s != seat_of(game, "guard"):
            assert not any(e["kind"] == "guard_action" for e in game.player_view(s)["events"])


@pytest.mark.parametrize("target_role", ["wolf", "villager", None])
def test_knight_once_or_pass_does_not_duplicate_speeches(target_role):
    game = GameEngine(38, configured("witch", "knight"))
    knight = seat_of(game, "knight")
    night(game, seat_of(game, "villager"), potion="save")
    while game.next_actor() != knight:
        s = game.next_actor()
        game.submit(s, simulated_action(game.player_view(s)))
    assert game.legal_actions(knight)[0]["kind"] == "duel"
    target = seat_of(game, target_role) if target_role else None
    game.submit(knight, Action("duel", target=target))
    if target_role == "wolf":
        assert not game.player_view(target)["alive"] and game.player_view(knight)["phase"] == "night"
        return
    if target_role == "villager":
        assert not game.player_view(knight)["alive"] and game.player_view(target)["alive"]
    else:
        assert game.legal_actions(knight)[0]["kind"] == "speak"
        assert not game.player_view(knight)["knight"]["used"]
    while game.player_view(1)["phase"] in {"day_speech", "knight_duel"}:
        s = game.next_actor()
        game.submit(s, simulated_action(game.player_view(s)))
    speeches = [e["data"]["seat"] for e in game.player_view(1)["events"] if e["kind"] == "speak"]
    assert len(speeches) == len(set(speeches))
    assert len(speeches) == 8 if target_role else len(speeches) == 9


@pytest.mark.asyncio
async def test_dead_hunter_prompt_is_permitted_and_other_dead_players_are_not():
    game = GameEngine(38, configured("witch", "hunter"))
    hunter = seat_of(game, "hunter")
    night(game, hunter)
    builder = PromptBuilder(Rulebook.load())
    connection = ModelConnection(id="m", provider="mock", model="offline", base_url="mock://local")
    profile = SeatProfile(seat=hunter, connection_id="m")
    request = builder.build(profile, connection, game.player_view(hunter, public_only=True))
    assert "被毒杀不能开枪" in request.messages[0].content
    assert "# 守卫" not in request.messages[0].content
    response = await MockAdapter().complete(request)
    assert json.loads(response.content) == {"kind": "shoot", "target": None}
    game.submit(hunter, Action("shoot"))
    with pytest.raises(RuleViolation):
        builder.build(profile, connection, game.player_view(hunter))


@pytest.mark.parametrize("values", [{"seers":0}, {"players":9}, {"players":9,"hunters":1,"guards":1}, {"hunters":2}])
def test_new_inputs_require_seer_and_exact_three_gods(values):
    with pytest.raises(ValidationError):
        RosterConfig(**values)


@pytest.mark.parametrize("extras", list(combinations(("witch", "hunter", "guard", "knight"), 2)))
def test_qualification_covers_only_selected_gods_and_keeps_independent_schemas(extras):
    suite = questions(asdict(configured(*extras)))
    expected = {f"r_{role}" for role in extras if role != "witch"}
    assert {q.id for q in suite if "_" in q.id} == expected
    assert len(suite) == 12 + len(expected)
    for q in suite:
        if q.id in expected:
            assert len(q.view["players"]) == 9
            assert q.view["legal_actions"][0]["allow_abstain"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role,extras", [("hunter",("witch","hunter")),("guard",("witch","guard")),("knight",("witch","knight"))])
async def test_human_new_roles_complete_via_api_and_hunter_window_survives_receipt_filter(tmp_path,role,extras):
    app = create_app(tmp_path / "gods.sqlite3", reading_scale=0, default_playback="auto")
    engine = GameEngine(38,configured(*extras))
    human = seat_of(engine,role)
    config = engine.config
    roster = {k:getattr(config,k) for k in ("players","wolves","seers","witches","hunters","guards","knights")}
    async with opened(app) as client:
        response = await client.post("/api/games",json={"roster":roster,"seed":38,"human_seat":human})
        assert response.status_code == 201,response.text
        gid = response.json()["game"]["game_id"]
        for _ in range(150):
            state = await ready(client,gid)
            if state["status"] == "finished":
                break
            assert state["status"] == "running"
            body = submit_body(state)
            posted = await client.post(f"/api/games/{gid}/actions",json=body)
            assert posted.status_code == 200,posted.text
            repeated = await client.post(f"/api/games/{gid}/actions",json=body)
            assert repeated.status_code == 200
        assert state["status"] == "finished"


def test_historical_nine_snapshot_and_no_seer_custom_snapshot_restore_unmodified():
    for config in (GameConfig(version="nine-player-v1",players=9,wolves=3,villagers=4),
                   GameConfig(version="custom-player-v1",players=9,wolves=1,seers=0,witches=0,villagers=8)):
        old = GameEngine(38,config).snapshot()
        old["snapshot_version"] = 3
        for key in ("hunters","guards","knights"):
            old["config"].pop(key)
        for key in ("last_guard","protected","knight_used","hunter_used","after_shot","speech_remaining"):
            old["state"].pop(key)
        restored = GameEngine.from_snapshot(old)
        assert restored.config == config
        assert restored.player_view(1)["events"] == GameEngine(38,config).player_view(1)["events"]
