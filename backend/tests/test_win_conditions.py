"""Side elimination, death windows and historical rule restoration; no model calls."""

import json
from dataclasses import replace

import pytest
from test_gods import configured, night, seat_of

from werewolf import Action, GameConfig, GameEngine, RuleViolation
from werewolf.player_state import PLAYER_PROTOCOL, PlayerState
from werewolf.profiles import ModelConnection, PersonaProfile, SeatProfile
from werewolf.prompts import ContextPolicy, PromptBuilder
from werewolf.roster import RosterConfig
from werewolf.rulebook import Rulebook


def position(*, wolves=1, villagers=3, god=None, extras=("witch", "hunter"), last_words=True):
    game = GameEngine(38, replace(configured(*extras), last_words=last_words))
    kept = set(game._living("wolf")[:wolves] + game._living("villager")[:villagers])
    kept.update([seat_of(game, god)] if god else [p["seat"] for p in game._state["players"]
                                               if p["role"] not in {"wolf", "villager"}])
    for player in game._state["players"]:
        player["alive"] = player["seat"] in kept
    game._start_night()
    return game


def skip_farewells(game):
    while not game.finished and game.legal_actions(game.next_actor())[0]["kind"] == "last_words":
        game.submit(game.next_actor(), Action("last_words", text=""))


@pytest.mark.parametrize("villagers,remaining", [(3, 6), (2, 5)])
def test_parity_and_wolf_majority_continue_when_both_sides_survive(villagers, remaining):
    game = position(wolves=3, villagers=villagers, god="seer")
    night(game, game._living("villager")[0])
    skip_farewells(game)
    assert not game.finished and len(game._living()) == remaining
    assert game.legal_actions(game.next_actor())[0]["kind"] == "speak"
    connection = ModelConnection(id="m", provider="mock", model="fixed", base_url="mock://local")
    profile = SeatProfile(seat=game.next_actor(), connection_id="m")
    request = PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL).build(profile, connection, game.player_view(profile.seat))
    payload = json.loads(request.messages[-1].content)
    assert payload["decision_context"]["max_possible_living_wolves"] == 3
    assert "不能据此推断狼少于好人" in payload["decision_context"]["notice"]


def test_last_villager_death_wins_for_wolves_only_after_farewell_even_with_fewer_wolves():
    game = position(villagers=1)
    victim = game._living("villager")[0]
    night(game, victim)
    assert not game.finished and game.next_actor() == victim
    restored = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
    for engine in (game, restored):
        engine.submit(victim, Action("last_words", text=""))
        assert engine.replay()["winner"] == "wolves"
        assert len(engine._living("wolf")) == 1 and len(engine._living()) == 4
    assert game.snapshot() == restored.snapshot()


@pytest.mark.parametrize("god,extras", [("seer", ("witch", "hunter")), ("witch", ("witch", "hunter")),
                                       ("hunter", ("witch", "hunter")), ("guard", ("guard", "knight")),
                                       ("knight", ("guard", "knight"))])
def test_each_god_counts_towards_the_god_side(god, extras):
    game = position(god=god, extras=extras)
    victim = seat_of(game, god)
    night(game, victim)
    assert not game.finished
    skip_farewells(game)
    if god == "hunter":
        assert not game.finished and game.legal_actions(victim)[0]["kind"] == "shoot"
        game.submit(victim, Action("shoot", target=None))
    assert game.replay()["winner"] == "wolves"
    assert len(game._living("wolf")) == 1 and len(game._living("villager")) == 3


def test_last_god_hunter_can_shoot_the_last_wolf_before_side_loss():
    game = position(god="hunter")
    hunter, wolf = seat_of(game, "hunter"), game._living("wolf")[0]
    night(game, hunter)
    skip_farewells(game)
    assert not game.finished
    restored = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
    for engine in (game, restored):
        engine.submit(hunter, Action("shoot", target=wolf))
        assert engine.replay()["winner"] == "good"
    assert game.snapshot() == restored.snapshot()


def test_simultaneous_last_god_and_last_wolf_death_is_good_victory():
    game = position(god="witch")
    witch, wolf = seat_of(game, "witch"), game._living("wolf")[0]
    night(game, witch, potion="poison", poison=wolf)
    assert not game.finished
    skip_farewells(game)
    assert game.replay()["winner"] == "good"
    assert not game._living("wolf") and len(game._living("villager")) == 3


def test_old_snapshot_preserves_parity_rules_and_public_configuration():
    game = GameEngine(38, GameConfig(win_condition="parity"))
    snapshot = game.snapshot()
    expected_rules = dict(snapshot["config"])
    expected_rules.pop("win_condition")
    snapshot["config"].pop("win_condition")
    snapshot["snapshot_version"] = 5
    restored = GameEngine.from_snapshot(json.loads(json.dumps(snapshot)))
    assert restored.config.win_condition == "parity"
    assert restored.player_view(1)["rules"] == expected_rules
    assert json.loads(json.dumps(restored.snapshot())) == json.loads(json.dumps(snapshot))
    for engine in (game, restored):
        for seat in engine._living("villager"):
            engine._player(seat)["alive"] = False
        assert engine._check_winner() and engine.replay()["winner"] == "wolves"
    assert game.replay() == restored.replay()
    assert RosterConfig().engine_config().win_condition == "eliminate_side"


@pytest.mark.parametrize("condition", ["", "unknown", True, [], None])
def test_invalid_win_condition_rejected(condition):
    with pytest.raises(RuleViolation):
        GameConfig(win_condition=condition)


@pytest.mark.parametrize("style", ["serious", "intuitive", "observer", "provocateur", "playful", "follower"])
def test_latest_good_check_survives_old_suspicion_performance_and_pruning(style):
    game = GameEngine(38)
    # These are genuine engine results, not a model-written memory.
    while game.player_view(1)["phase"] == "night":
        seat = game.next_actor()
        kind = game.legal_actions(seat)[0]["kind"]
        action = Action(kind, target=4 if kind == "inspect" else 6,
                        text="统一刀口" if kind == "propose_kill" else None) if kind != "witch" else Action(kind, potion="save", target=6)
        game.submit(seat, action)
    while game.next_actor() != 3:
        game.submit(game.next_actor(), Action("speak", text="听你说"))
    view = game.player_view(3)
    state = PlayerState(stance="4号查杀必出", claim="seer", intent="试探4号")
    connection = ModelConnection(id="m", provider="mock", model="fixed", base_url="mock://local")
    profile = SeatProfile(seat=3, connection_id="m", persona=PersonaProfile(play_style=style))
    request = PromptBuilder(Rulebook.load(), ContextPolicy(soft_char_limit=1), protocol=PLAYER_PROTOCOL,
                            player_state=state).build(profile, connection, view)
    payload = json.loads(request.messages[-1].content)
    assert payload["state"]["checks"] == [{"day": 1, "target": 4, "is_wolf": False}]
    assert payload["player_state"]["stance"] == "4号查杀必出"
    assert not payload["history"] and "优先于口述与旧印象" in request.messages[0].content
    assert "诈报只是表演，不能记成真经历" in request.messages[0].content
    assert payload["persona"]["play_style"] == style
    assert "objective" not in payload["communication"] and "play_style_guidance" not in payload
