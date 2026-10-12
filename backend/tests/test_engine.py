import json

import pytest

from werewolf import Action, GameConfig, GameEngine, RuleViolation
from werewolf.demo import run_demo, simulated_action


@pytest.fixture
def game():
    engine = GameEngine(38, GameConfig.legacy())  # Keep old-rule regression fixtures intact.
    assert [engine.player_view(s)["role"] for s in range(1, 7)] == [
        "wolf", "wolf", "seer", "villager", "villager", "villager",
    ]
    return engine


def night(game, target=6):
    while game.player_view(1)["phase"] == "night":
        seat = game.next_actor()
        kind = game.legal_actions(seat)[0]["kind"]
        action = Action(kind, text="协调目标") if kind == "wolf_message" else Action(
            kind, target=game.legal_actions(seat)[0]["targets"][0] if kind == "inspect" else target,
            text="提案理由" if kind == "propose_kill" else None,
        )
        game.submit(seat, action)


def speeches(game):
    while game.player_view(1)["phase"] in {"day_speech", "runoff_speech"}:
        game.submit(game.next_actor(), Action("speak", text="我的公开发言"))


def votes(game, mapping):
    for seat, target in mapping.items():
        assert game.next_actor() == seat
        game.submit(seat, Action("vote", target=target))


def test_roles_and_hidden_night_information(game):
    view = game.player_view(4)
    assert view["wolf_teammates"] == []
    assert view["legal_actions"] == []
    assert all(set(p) == {"seat", "alive"} for p in view["players"])
    assert not {"seed", "rng_state", "current_actor", "revision", "pending_kill"} & view.keys()
    assert game.player_view(1)["wolf_teammates"] == [2]
    game.submit(1, Action("propose_kill", target=4, text="秘密暗号"))
    assert game.player_view(4) == view
    assert all(e["kind"] != "wolf_proposals" for e in game.player_view(2)["events"])
    game.submit(2, Action("propose_kill", target=5, text="另一个意见"))
    assert game.player_view(4) == view
    assert game.player_view(2)["events"][-1]["data"]["proposals"]["1"]["text"] == "秘密暗号"
    with pytest.raises(RuleViolation):
        game.replay()


def test_night_is_simultaneous_and_checks_are_private(game):
    night(game, target=3)
    assert not game.player_view(3)["alive"]
    assert game.player_view(3)["checks"] == [{"day": 1, "target": 1, "is_wolf": True}]
    assert game.player_view(3)["legal_actions"] == []
    for seat in [1, 2, 4, 5, 6]:
        assert game.player_view(seat)["checks"] == []
        assert all(e["kind"] != "inspection" for e in game.player_view(seat)["events"])
    assert not game.finished  # Losing the only seer is not the parity win condition.


def test_good_victory(game):
    night(game)
    speeches(game)
    votes(game, {1: 2, 2: 1, 3: 1, 4: 1, 5: 1})
    night(game, target=5)
    speeches(game)
    votes(game, {2: 3, 3: 2, 4: 2})
    assert game.replay()["winner"] == "good"
    assert game.next_actor() is None
    assert all(not game.legal_actions(s) for s in range(1, 7))
    assert len(game.player_view(4)["revealed_roles"]) == 6


def test_wolf_victory_after_day_elimination(game):
    night(game)
    speeches(game)
    votes(game, {1: 4, 2: 4, 3: 4, 4: 1, 5: 4})
    assert game.replay()["winner"] == "wolves"


def test_abstain_then_night_parity_and_dead_seer_skip(game):
    night(game, target=3)
    speeches(game)
    votes(game, {1: None, 2: None, 4: None, 5: None, 6: None})
    night(game, target=6)
    assert game.replay()["winner"] == "wolves"
    assert sum(e["kind"] == "inspection" for e in game.replay()["events"]) == 1


@pytest.mark.parametrize("runoff,eliminated", [
    ({3: 1, 4: 1, 5: 2}, 1),
    ({3: 1, 4: 2, 5: None}, None),
    ({3: None, 4: None, 5: None}, None),
])
def test_tie_and_single_runoff(game, runoff, eliminated):
    night(game)
    speeches(game)
    votes(game, {1: 2, 2: 1, 3: 1, 4: 2, 5: None})
    assert game.next_actor() == 1
    assert game.player_view(4)["phase"] == "runoff_speech"
    speeches(game)
    assert game.legal_actions(1) == []
    assert game.legal_actions(3)[0]["targets"] == [1, 2]
    votes(game, runoff)
    assert game.player_view(4)["phase"] == "night"
    assert game.player_view(4)["day"] == 2
    deaths = [p["seat"] for p in game.player_view(4)["players"] if not p["alive"]]
    assert deaths == ([eliminated, 6] if eliminated else [6])


def test_everyone_tied_no_eligible_runoff_voters(game):
    night(game)
    speeches(game)
    votes(game, {1: 2, 2: 3, 3: 4, 4: 5, 5: 1})
    speeches(game)
    assert game.player_view(4)["day"] == 2
    assert game.player_view(4)["phase"] == "night"


def test_ballots_remain_secret_until_all_submitted(game):
    night(game)
    speeches(game)
    game.submit(1, Action("vote", target=2))
    assert not any(e["kind"] == "vote_result" for e in game.player_view(4)["events"])
    votes(game, {2: 1, 3: 1, 4: 1, 5: 1})
    result = next(e for e in game.player_view(4)["events"] if e["kind"] == "vote_result")
    assert result["data"]["votes"] == {"1": 2, "2": 1, "3": 1, "4": 1, "5": 1}


@pytest.mark.parametrize("action", [
    Action("kill", target=4), Action("propose_kill", target=4, text=""),
    Action("propose_kill", target=4, text=" "), Action("propose_kill", target=4, text="字" * 151),
    Action("propose_kill", text="hi"), {"kind": "propose_kill"},
])
def test_invalid_action_does_not_mutate(game, action):
    before = game.snapshot()
    with pytest.raises(RuleViolation):
        game.submit(1, action)
    assert game.snapshot() == before


@pytest.mark.parametrize("target", [None, 1, 2, 0, 7, True, "4", 4.0])
def test_wolf_cannot_kill_invalid_targets(game, target):
    before = game.snapshot()
    with pytest.raises(RuleViolation):
        game.submit(1, Action("propose_kill", target=target, text="提案"))
    assert game.snapshot() == before


def test_wrong_actor_dead_player_self_vote_and_dead_target(game):
    with pytest.raises(RuleViolation):
        game.submit(4, Action("wolf_message", text="作弊"))
    night(game)
    with pytest.raises(RuleViolation):
        game.submit(6, Action("speak", text="遗言"))
    with pytest.raises(RuleViolation):
        game.submit(1, Action("speak", text="字" * 301))
    speeches(game)
    for target in [1, 6]:
        with pytest.raises(RuleViolation):
            game.submit(1, Action("vote", target=target))


def test_views_and_snapshots_are_detached(game):
    before = game.snapshot()
    view = game.player_view(1)
    view["players"][0]["alive"] = False
    view["events"][0]["data"]["players"] = 99
    snapshot = game.snapshot()
    snapshot["state"]["players"][0]["role"] = "villager"
    assert game.snapshot() == before


def test_disagreeing_wolves_and_json_restore_preserve_rng(game):
    game.submit(1, Action("propose_kill", target=4, text="a"))
    game.submit(2, Action("propose_kill", target=5, text="b"))
    game.submit(1, Action("wolf_message", text="a"))
    game.submit(2, Action("wolf_message", text="b"))
    game.submit(1, Action("kill", target=4))
    restored = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
    for engine in [game, restored]:
        engine.submit(2, Action("kill", target=5))
        assert all(p["alive"] for p in engine.player_view(4)["players"])
        engine.submit(3, Action("inspect", target=2))
    assert game.snapshot() == restored.snapshot()
    assert next(p["seat"] for p in game.player_view(4)["players"] if not p["alive"]) in [4, 5]


def test_restore_every_action_boundary_and_determinism():
    game = GameEngine(38)
    for _ in range(200):
        clone = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
        assert all(game.player_view(s) == clone.player_view(s) for s in range(1, 7))
        if game.finished:
            assert game.replay() == clone.replay()
            break
        seat = game.next_actor()
        action = simulated_action(game.player_view(seat))
        game.submit(seat, action)
        clone.submit(seat, action)
        assert game.snapshot() == clone.snapshot()
    else:
        pytest.fail("Game failed to finish")
    assert run_demo(38).replay() == game.replay()


def test_many_seeded_games_finish_with_consistent_winner():
    winners = set()
    for seed in range(100):
        game = run_demo(seed)
        replay = game.replay()
        wolves = sum(p["alive"] and p["role"] == "wolf" for p in replay["players"])
        villagers = sum(p["alive"] and p["role"] == "villager" for p in replay["players"])
        gods = sum(p["alive"] and p["role"] not in {"wolf", "villager"} for p in replay["players"])
        assert wolves == 0 if replay["winner"] == "good" else wolves > 0 and (villagers == 0 or gods == 0)
        winners.add(replay["winner"])
    assert winners == {"good", "wolves"}


def test_invalid_configuration_snapshot_and_action_shape():
    with pytest.raises(RuleViolation):
        GameConfig(players=9)
    with pytest.raises(RuleViolation):
        GameEngine(True)
    with pytest.raises(RuleViolation):
        GameEngine.from_snapshot({"snapshot_version": 99})
    for data in [{}, {"kind": "vote", "role": "wolf"}, []]:
        with pytest.raises(RuleViolation):
            Action.from_dict(data)
    assert Action.from_dict({"kind": "vote", "target": None}) == Action("vote")


def test_unanimous_proposals_skip_reply_and_vote(game):
    game.submit(1, Action("propose_kill", target=6, text="字" * 150))
    game.submit(2, Action("propose_kill", target=6, text="同意"))
    assert game.next_actor() == 3
    assert game.legal_actions(3)[0]["kind"] == "inspect"
    events = game.player_view(1)["events"]
    assert events[-1]["data"]["method"] == "unanimous_proposals"
    assert not any(e["kind"] == "wolf_replies" for e in events)


def test_replies_are_sealed_and_limited_to_one_per_wolf(game):
    game.submit(1, Action("propose_kill", target=4, text="我的提案"))
    game.submit(2, Action("propose_kill", target=5, text="我的提案"))
    game.submit(1, Action("wolf_message", text="字" * 150))
    assert not any(e["kind"] == "wolf_replies" for e in game.player_view(2)["events"])
    with pytest.raises(RuleViolation):
        game.submit(1, Action("wolf_message", text="再次发言"))
    before = game.snapshot()
    with pytest.raises(RuleViolation):
        game.submit(2, Action("wolf_message", text="字" * 151))
    assert before == game.snapshot()
    game.submit(2, Action("wolf_message", text="我改变主意了"))
    assert game.player_view(1)["events"][-1]["kind"] == "wolf_replies"
    for seat in [3, 4, 5, 6]:
        assert not any(e["kind"].startswith("wolf_") for e in game.player_view(seat)["events"])
    before = game.snapshot()
    with pytest.raises(RuleViolation):
        game.submit(1, Action("kill", target=4, text="最终票不附理由"))
    assert before == game.snapshot()
    game.submit(1, Action("kill", target=4))
    assert not any(e["kind"] == "wolf_decision" for e in game.player_view(2)["events"])
    game.submit(2, Action("kill", target=4))
    assert game.player_view(1)["events"][-1]["data"]["target"] == 4


def test_lone_wolf_skips_all_discussion(game):
    night(game)
    speeches(game)
    votes(game, {1: 2, 2: 1, 3: 1, 4: 1, 5: 1})
    assert game.next_actor() == 2
    assert game.legal_actions(2)[0]["kind"] == "kill"
    game.submit(2, Action("kill", target=5))
    assert game.next_actor() == 3
    assert not any(e["kind"] == "wolf_decision" and e["day"] == 2
                   for e in game.player_view(1)["events"])


def test_partial_proposals_and_replies_survive_json_restore(game):
    sequence = [
        (1, Action("propose_kill", target=4, text="a")),
        (2, Action("propose_kill", target=5, text="b")),
        (1, Action("wolf_message", text="保持意见")),
        (2, Action("wolf_message", text="保持意见")),
        (1, Action("kill", target=4)), (2, Action("kill", target=5)),
        (3, Action("inspect", target=1)),
    ]
    for seat, action in sequence:
        restored = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
        game.submit(seat, action)
        restored.submit(seat, action)
        assert restored.snapshot() == game.snapshot()


def test_previous_rules_snapshot_is_explicitly_rejected(game):
    data = game.snapshot()
    data["snapshot_version"] = 1
    with pytest.raises(RuleViolation, match="存档版本"):
        GameEngine.from_snapshot(data)
