"""New preset, simultaneous night resolution, private skills and legacy saves."""

import json
from dataclasses import asdict

import pytest

from werewolf import Action, GameConfig, GameEngine, RuleViolation
from werewolf.agent import InvalidModelResponse, PlayerAgent, parse_action
from werewolf.demo import simulated_action
from werewolf.mock_adapter import MockAdapter
from werewolf.profiles import ModelConnection, SeatProfile, content_hash
from werewolf.prompts import PromptBuilder
from werewolf.qualification_questions import questions
from werewolf.rulebook import Rulebook


def before_witch(game, victim=6):
    while game.player_view(1)["phase"] == "night":
        seat = game.next_actor()
        spec = game.legal_actions(seat)[0]
        if spec["kind"] == "witch":
            return seat
        game.submit(seat, Action(spec["kind"], target=spec["targets"][0] if spec["kind"] == "inspect" else victim,
                                 text="共同提案" if spec["kind"] == "propose_kill" else None))
    return None


def abstain_day(game):
    while game.player_view(1)["phase"] in {"day_speech", "vote"}:
        kind = game.legal_actions(game.next_actor())[0]["kind"]
        game.submit(game.next_actor(), Action(kind, text="公开讨论" if kind == "speak" else None))


def test_new_default_replaces_exactly_one_villager():
    game = GameEngine(38)
    assert [game.player_view(s)["role"] for s in range(1, 7)] == [
        "wolf", "wolf", "seer", "villager", "witch", "villager",
    ]
    assert game.player_view(1)["rules"]["version"] == "six-player-v3-witch"
    assert game.player_view(1)["events"][0]["data"]["roster"] == {
        "wolf": 2, "seer": 1, "witch": 1, "villager": 2,
    }


def test_antidote_creates_peaceful_night_without_leaking_reason_or_stock():
    game = GameEngine(38)
    assert before_witch(game) == 5
    public_before = game.player_view(4)
    assert public_before["phase"] == "night" and "current_actor" not in public_before
    assert game.player_view(5)["witch"]["night_victim"] == 6
    game.submit(5, Action("witch", target=6, potion="save"))
    assert all(p["alive"] for p in game.player_view(4)["players"])
    assert game.player_view(4)["events"][-1]["data"] == {"dead": []}
    assert game.player_view(5)["witch"] == {"potions": {"save": False, "poison": True}}
    for seat in [1, 2, 3, 4, 6]:
        view = game.player_view(seat)
        assert "witch" not in view
        assert not any(e["kind"] == "witch_action" for e in view["events"])
        assert "potions" not in json.dumps(view)
    assert "witch" not in game.player_view(5, public_only=True)


@pytest.mark.parametrize("potion,target,dead", [
    ("skip", None, [6]), ("poison", 1, [1, 6]), ("poison", 6, [6]),
])
def test_night_deaths_resolve_once_without_revealing_causes(potion, target, dead):
    game = GameEngine(38)
    before_witch(game)
    game.submit(5, Action("witch", potion=potion, target=target))
    dawn = game.player_view(4)["events"][-1]
    assert dawn["kind"] == "dawn" and dawn["data"] == {"dead": dead}
    assert all(not game.player_view(seat)["alive"] for seat in dead)


@pytest.mark.parametrize("action", [
    Action("witch", potion="save", target=4), Action("witch", potion="poison", target=5),
    Action("witch", potion="skip", target=6), Action("witch", potion="save"),
    Action("witch", potion="poison", target=True), Action("witch", potion="poison", target=7),
    Action("witch", potion="save", target=6, text="额外解释"),
    Action("witch", potion="both", target=6), Action("witch", target=6),
])
def test_invalid_potion_choice_is_atomic(action):
    game = GameEngine(38)
    before_witch(game)
    before = game.snapshot()
    with pytest.raises(RuleViolation):
        game.submit(5, action)
    assert game.snapshot() == before


def test_witch_cannot_self_save_but_can_poison_on_her_death_night():
    game = GameEngine(38)
    before_witch(game, 5)
    assert "save" not in game.legal_actions(5)[0]["potions"]
    with pytest.raises(RuleViolation):
        game.submit(5, Action("witch", potion="save", target=5))
    game.submit(5, Action("witch", potion="poison", target=1))
    assert not game.player_view(5)["alive"] and not game.player_view(1)["alive"]
    assert game.legal_actions(5) == []
    assert "witch" not in game.player_view(5, public_only=True)


def test_winner_checked_only_after_both_knife_and_poison_not_in_between():
    game = GameEngine(38, GameConfig(win_condition="parity"))  # Preserve historical parity regression.
    before_witch(game)
    game.submit(5, Action("witch", potion="save", target=6))
    while game.player_view(1)["phase"] == "day_speech":
        game.submit(game.next_actor(), Action("speak", text="讨论"))
    while game.player_view(1)["phase"] == "vote":
        seat = game.next_actor()
        game.submit(seat, Action("vote", target=3 if seat == 4 else 4))
    assert before_witch(game, 6) == 5
    assert "night_victim" not in game.player_view(5)["witch"]  # Antidote spent.
    assert "save" not in game.legal_actions(5)[0]["potions"]
    game.submit(5, Action("witch", potion="poison", target=1))
    assert not game.finished  # Knife alone would be 2 wolves vs 2 good; poison changes it.
    assert game.player_view(3)["events"][-1]["data"] == {"dead": [1, 6]}
    assert game.player_view(5)["witch"]["potions"] == {"save": False, "poison": False}
    abstain_day(game)
    assert before_witch(game, 3) is None  # No extra decision with no abilities remaining.
    assert len([e for e in game.player_view(5)["events"] if e["kind"] == "witch_action"]) == 2


def test_used_poison_does_not_disable_remaining_antidote_and_dead_targets_excluded():
    game = GameEngine(38)
    before_witch(game)
    game.submit(5, Action("witch", potion="poison", target=1))
    abstain_day(game)
    assert before_witch(game, 4) == 5
    spec = game.legal_actions(5)[0]
    assert spec["potions"] == {"skip": [], "save": [4]}
    assert game.player_view(5)["witch"]["night_victim"] == 4
    game.submit(5, Action("witch", potion="save", target=4))
    assert game.player_view(4)["alive"]


def test_snapshot_restores_pending_witch_and_rng_exactly():
    game = GameEngine(38)
    before_witch(game)
    clone = GameEngine.from_snapshot(json.loads(json.dumps(game.snapshot())))
    assert clone.player_view(5) == game.player_view(5)
    for engine in [game, clone]:
        engine.submit(5, Action("witch", potion="poison", target=2))
    assert clone.snapshot() == game.snapshot()


def test_old_version_two_snapshot_keeps_three_villagers_and_no_new_skills():
    legacy = GameEngine(38, GameConfig.legacy())
    old = legacy.snapshot()
    old["snapshot_version"] = 2
    old["config"].pop("witches")
    for key in ["potions", "pending_save", "pending_poison"]:
        old["state"].pop(key)
    old["state"]["events"][0]["data"].pop("roster")
    restored = GameEngine.from_snapshot(json.loads(json.dumps(old)))
    assert restored.config == GameConfig.legacy()
    assert all(p["role"] != "witch" for p in restored.snapshot()["state"]["players"])
    while not restored.finished:
        seat = restored.next_actor()
        assert restored.legal_actions(seat)[0]["kind"] != "witch"
        restored.submit(seat, simulated_action(restored.player_view(seat)))


@pytest.mark.asyncio
async def test_witch_agent_reads_only_own_rules_has_one_call_and_strict_protocol():
    game = GameEngine(38)
    before_witch(game)
    connection = ModelConnection(id="mock", provider="mock", model="offline", base_url="mock://local")
    builder = PromptBuilder(Rulebook.load())
    agent = PlayerAgent(SeatProfile(seat=5, connection_id="mock"), connection, builder)
    request = agent.prepare(game.player_view(5))
    state = json.loads(request.messages[-1].content)["state"]
    assert state["witch"]["night_victim"] == 6
    assert request.max_output_tokens == 256
    assert '女巫' in request.messages[0].content and '全局一次' in request.messages[0].content
    assert {name for name, _ in Rulebook.load().select("witch", "witch")} == {
        "游戏.md", "roles/werewolf-witch/身份.md", "规则.md",
    }
    action = await agent.decide(game.player_view(5), MockAdapter())
    assert action == Action("witch", target=6, potion="save") and len(agent.records) == 1
    spec = game.legal_actions(5)[0]
    for invalid in [{"kind": "witch", "potion": "skip", "target": 6},
                    {"kind": "witch", "potion": "save", "target": 1},
                    {"kind": "witch", "potion": "poison", "target": 5},
                    {"kind": "witch", "potion": "save", "target": 6, "text": "解释"}]:
        with pytest.raises(InvalidModelResponse):
            parse_action(json.dumps(invalid), spec)
    game.submit(5, action)
    # Same shared connection, different player's day speech never sees potions/victim.
    other = builder.build(SeatProfile(seat=1, connection_id="mock"), connection, game.player_view(1))
    other_data = json.loads(other.messages[-1].content)
    assert "witch" not in other_data["state"]
    assert not any(e["kind"] == "witch_action" for e in other_data["facts"])


def test_new_qualification_has_witch_protocol_and_old_rules_have_different_fingerprint():
    suite = questions()
    question = next(q for q in suite if q.view["role"] == "witch")
    spec = question.view["legal_actions"][0]
    assert len(suite) == 12 and "save" not in spec["potions"]
    for answer in [{"kind": "witch", "potion": "save", "target": 2},
                   {"kind": "witch", "potion": "poison", "target": 4}]:
        with pytest.raises(InvalidModelResponse):
            parse_action(json.dumps(answer), spec)
    assert content_hash(asdict(GameConfig())) != content_hash(asdict(GameConfig.legacy()))


@pytest.mark.asyncio
async def test_old_action_receipt_remains_idempotent_after_adding_potion_field(tmp_path):
    from app_helpers import flow_app as create_app
    from test_api import opened

    app = create_app(tmp_path / "receipts.sqlite3")
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38})).json()["game"]
        runtime = app.state.service.games[created["game_id"]]
        await app.state.service.control(runtime, "pause", "pause")
        before = runtime.engine.snapshot()
        old_action = {"kind": "propose_kill", "target": 6, "text": "旧操作"}
        payload = {"operation": "action", "expected_version": "old-version", "action": old_action}
        response = runtime.visible()
        # Old code used asdict(Action) with exactly these three fields, including nulls.
        app.state.service.store.commit(runtime.id, runtime.snapshot(), response,
                                       ("old-operation", content_hash(payload), response))
        duplicate = await client.post(f"/api/games/{runtime.id}/actions", json={
            "operation_id": "old-operation", "expected_version": "old-version", "action": old_action,
        })
        assert duplicate.status_code == 200 and duplicate.json() == response
        assert runtime.engine.snapshot() == before
