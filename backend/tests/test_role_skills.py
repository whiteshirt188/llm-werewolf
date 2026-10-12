"""Facts and AI action foundations; scripted/offline, with no hidden-role input."""

import json
import random
from copy import deepcopy

import pytest

from werewolf.agent import InvalidModelResponse, ModelResponse, PlayerAgent, parse_decision
from werewolf.demo import simulated_action
from werewolf.engine import Action, GameConfig, GameEngine
from werewolf.player_logic import constrain_action, decision_facts
from werewolf.player_state import PLAYER_PROTOCOL, PlayerState
from werewolf.profiles import ModelConnection, PersonaProfile, SeatProfile, canonical_json
from werewolf.prompts import (
    FACTION_PLAYER_PROMPT_VERSION,
    PLAYER_PROMPT_VERSION,
    ContextPolicy,
    PromptBuilder,
)
from werewolf.rulebook import Rulebook, RulebookError


def event(kind, data, *, day=1, event_id=1):
    return {"id": event_id, "day": day, "kind": kind, "data": data}


def view_for(role="witch", seat=8, living=(1, 5, 8), phase="day_vote", kind="vote"):
    """Same visible endgame as the reported game, without a hidden-role roster."""
    view = {
        "rules": GameConfig(version="nine-player-v2-gods", players=9, wolves=3, hunters=1, villagers=3, last_words=True).public_rules(),
        "seat": seat, "role": role, "alive": seat in living, "day": 3, "phase": phase,
        "players": [{"seat": s, "alive": s in living} for s in range(1, 10)],
        "checks": [], "wolf_teammates": [], "winner": None,
        "legal_actions": [{"kind": kind, "targets": [s for s in living if s != seat], "allow_abstain": True}],
        "events": [
            event("witch_action", {"potion": "save", "target": 1}, event_id=1),
            event("vote_result", {"round": 1, "votes": {"1": 2, "2": 7, "3": 7, "4": 7,
                  "5": 7, "6": 7, "7": 4, "8": 2, "9": 2}, "counts": {"7": 5, "2": 3, "4": 1}}, event_id=2),
            event("last_words", {"seat": 7, "text": "7号，村民。4号验错了。"}, event_id=3),
            event("speak", {"seat": 1, "text": "7翻牌村民，2、8、9昨天都投我。"}, day=2, event_id=4),
            event("vote_result", {"round": 1, "votes": {"1": 2, "2": 1, "3": 2, "5": 2,
                  "6": 2, "8": 2, "9": 2}, "counts": {"2": 6, "1": 1}}, day=2, event_id=5),
            event("last_words", {"seat": 2, "text": "2号，村民。"}, day=2, event_id=6),
            event("hunter_shot", {"seat": 6, "target": 9}, day=3, event_id=7),
        ],
    }
    if role == "witch":
        view["witch"] = {"potions": {"save": False, "poison": False}}
    else:
        # Only the witch sees her private use record.
        view["events"] = view["events"][1:]
    return view


def request_for(view, *, prompt_version=None, state=None, policy=None, book=None, style="serious"):
    connection = ModelConnection(id="mock", provider="mock", model="fixed", base_url="mock://local")
    profile = SeatProfile(seat=view["seat"], connection_id="mock", persona=PersonaProfile(play_style=style))
    builder = PromptBuilder(book or Rulebook.load(), policy, protocol=PLAYER_PROTOCOL,
                            player_state=state, prompt_version=prompt_version)
    return builder.build(profile, connection, view)


def test_claims_and_hunter_victim_never_become_public_revealed_roles():
    view = view_for("villager", 1)
    facts = decision_facts(view)
    assert facts["public_role_reveals"] == {"6": "hunter"}
    assert facts["known_roles"] == {"1": "villager", "6": "hunter"}
    assert {2, 7, 9} <= set(facts["dead_without_public_role_reveal"])
    assert 2 not in facts["confirmed_good"] and 7 not in facts["confirmed_good"]
    assert 9 not in facts["confirmed_wolves"]
    assert facts["recent_vote_results"][0]["votes"]["8"] == 2
    assert facts["recent_vote_results"][0]["votes"]["9"] == 2
    assert facts["own_votes"] == [{"day": 1, "round": 1, "target": 2, "event_id": 2},
                                 {"day": 2, "round": 1, "target": 2, "event_id": 5}]


def test_saved_good_information_solves_witch_endgame_and_stays_private():
    witch = view_for()
    before = deepcopy(witch)
    request = request_for(witch, state=PlayerState(stance="7翻牌村民，1像狼", claim="villager"),
                          policy=ContextPolicy(soft_char_limit=1))
    payload = json.loads(request.messages[-1].content)
    assert payload["decision_facts"]["confirmed_good"] == [1, 6, 8]
    assert payload["decision_facts"]["confirmed_wolves"] == [5]
    assert payload["state"]["role"] == "witch" and payload["player_state"]["claim"] == "villager"
    assert payload["state"]["legal_actions"][0]["targets"] == [5]
    assert payload["history"] == []  # factual board survives all speech pruning
    assert payload["decision_facts"]["recent_vote_results"][0]["votes"]["8"] == 2
    assert witch == before and witch["legal_actions"][0]["targets"] == [1, 5]
    other = json.loads(request_for(view_for("villager", 1)).messages[-1].content)
    assert 8 not in other["decision_facts"]["confirmed_good"]
    assert "witch" not in other["state"]
    assert not any(s["source"].startswith("own_saved") for s in other["decision_facts"]["sources"])
    assert "werewolf-witch/身份.md" in request.messages[0].content
    assert "项目规则上下文：roles/werewolf-wolf/身份.md" not in request.messages[0].content


def test_ai_cannot_vote_her_saved_good_but_public_engine_permissions_are_preserved():
    view = view_for()
    request = request_for(view)
    state = json.loads(request.messages[-1].content)["state"]
    with pytest.raises(InvalidModelResponse):
        parse_decision(canonical_json({"action": {"kind": "vote", "target": 1}, "player_update": {}}), request, state)
    action, update, error = parse_decision(
        canonical_json({"action": {"kind": "vote", "target": 5}, "player_update": {}}), request, state)
    assert action == Action("vote", target=5) and update == {} and error is None
    assert view["legal_actions"][0]["targets"] == [1, 5]


@pytest.mark.parametrize("phase", ["last_words", "hunter_shot"])
def test_pending_death_windows_do_not_infer_a_last_living_wolf(phase):
    facts = decision_facts(view_for(phase=phase))
    assert facts["confirmed_good"] == [1, 6, 8]
    assert facts["confirmed_wolves"] == []


def test_wolf_team_remains_real_despite_teammates_fake_farewell():
    view = view_for("wolf", 5)
    view["wolf_teammates"] = [2, 7]
    facts = decision_facts(view)
    assert facts["confirmed_wolves"] == [2, 5, 7]
    assert facts["confirmed_good"] == [1, 3, 4, 6, 8, 9]
    assert "2" not in facts["public_role_reveals"]
    # Voting a known teammate stays available for strategic bussing.
    spec = {"kind": "vote", "targets": [1, 2, 7, 8], "allow_abstain": True}
    assert constrain_action(spec, view, facts) == spec


def test_real_knight_results_confirm_targets_but_hunter_shots_do_not():
    view = view_for("villager", 1, living=(1, 4, 5, 8, 9))
    view["events"] += [event("knight_duel", {"seat": 3, "target": 2, "success": True}, day=2, event_id=8),
                       event("knight_duel", {"seat": 7, "target": 8, "success": False}, day=3, event_id=9)]
    facts = decision_facts(view)
    assert {3, 7, 8} <= set(facts["confirmed_good"])
    assert facts["confirmed_wolves"] == [2]
    assert facts["known_roles"]["2"] == "wolf" and facts["known_roles"]["7"] == "knight"
    assert 9 not in facts["confirmed_wolves"]


@pytest.mark.parametrize("kind", ["vote", "shoot", "duel"])
def test_attack_targets_respect_own_checks_even_when_memory_disagrees(kind):
    view = view_for("seer", 4, living=(1, 2, 3, 4, 5, 6), kind=kind)
    view["checks"] = [{"day": 1, "target": 3, "is_wolf": False},
                      {"day": 2, "target": 2, "is_wolf": True}]
    facts = decision_facts(view)
    assert constrain_action(view["legal_actions"][0], view, facts)["targets"] == [2]


def test_witch_poison_excludes_visible_knife_and_saved_good_without_forcing_use():
    view = view_for(living=(1, 3, 5, 6, 8, 9), phase="night", kind="witch")
    view["events"] = view["events"][:-1]  # Before the third-night deaths and hunter shot.
    view["witch"] = {"potions": {"save": True, "poison": True}, "night_victim": 6}
    view["legal_actions"] = [{"kind": "witch", "potions": {"skip": [], "save": [6], "poison": [1, 3, 5, 6, 9]}}]
    payload = json.loads(request_for(view).messages[-1].content)
    assert payload["state"]["legal_actions"][0]["potions"] == {"skip": [], "save": [6], "poison": [3, 5, 9]}
    # No speculative faction assigned to the human's short speech or to the gun victim.
    assert 3 not in payload["decision_facts"]["confirmed_wolves"]
    assert 9 not in payload["decision_facts"]["confirmed_wolves"]
    facts = {"confirmed_good": [1, 3, 5, 6, 9], "confirmed_wolves": []}
    assert constrain_action(view["legal_actions"][0], view, facts)["potions"] == {"skip": [], "save": [6]}


def test_seer_prefers_new_checks_with_repeat_fallback_and_only_good_runoff_abstains():
    view = view_for("seer", 4, living=(1, 3, 4, 5), kind="inspect")
    view["checks"] = [{"target": 1, "is_wolf": False}, {"target": 3, "is_wolf": False}]
    spec = constrain_action(view["legal_actions"][0], view, decision_facts(view))
    assert spec["targets"] == [5]
    view["checks"].append({"target": 5, "is_wolf": True})
    assert constrain_action(view["legal_actions"][0], view, decision_facts(view))["targets"] == [1, 3, 5]
    view["legal_actions"] = [{"kind": "vote", "targets": [1, 3], "allow_abstain": True}]
    spec = constrain_action(view["legal_actions"][0], view, decision_facts(view))
    assert spec["targets"] == [] and spec["allow_abstain"]
    assert simulated_action({**view, "legal_actions": [spec]}) == Action("vote", target=None)


def test_known_wolf_upper_bound_can_confirm_other_good_only_with_true_sources():
    view = view_for("seer", 4, living=(1, 3, 4, 5))
    view["checks"] = [{"target": 2, "is_wolf": True}, {"target": 7, "is_wolf": True},
                      {"target": 5, "is_wolf": True}]
    facts = decision_facts(view)
    assert {1, 3, 4} <= set(facts["confirmed_good"])
    assert facts["confirmed_wolves"] == [2, 5, 7]
    view["checks"] = []
    facts = decision_facts(view)
    assert 3 not in facts["confirmed_good"]


@pytest.mark.asyncio
async def test_valid_new_decision_takes_one_call_and_unchanged_output_budget():
    view = view_for()
    connection = ModelConnection(id="mock", provider="mock", model="fixed", base_url="mock://local")
    profile = SeatProfile(seat=8, connection_id="mock")
    agent = PlayerAgent(profile, connection, PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL))

    class Script:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            assert request.max_output_tokens == 256
            return ModelResponse(canonical_json({"action": {"kind": "vote", "target": 5},
                                                "player_update": {"reply_to": 999999}}), simulated=True)

    script = Script()
    assert await agent.decide(view, script) == Action("vote", target=5)
    assert script.calls == 1 and agent.records[-1].player_update_error == "invalid_player_update"


def test_frozen_legacy_role_paths_and_v7_prompt_are_not_upgraded():
    current = Rulebook.load(legacy=True)
    # Freeze existing document contents under the old manifest to test selection
    # without relying on a mutable on-disk legacy copy.
    old_paths = {f"roles/werewolf-{role}/SKILL.md": f"references/roles/{role}.md"
                 for role in ("wolf", "seer", "witch", "hunter", "guard", "knight", "villager")}
    old_files = tuple((old_paths.get(name, name), text) for name, text in current.files)
    frozen = Rulebook(old_files)
    assert {name for name, _ in frozen.select("witch", "vote")} == {
        "SKILL.md", "references/roles/witch.md", "references/phases/day.md"}
    view = view_for()
    request = request_for(view, book=frozen, prompt_version=FACTION_PLAYER_PROMPT_VERSION, style="playful")
    payload = json.loads(request.messages[-1].content)
    assert "decision_facts" not in payload
    assert payload["state"]["legal_actions"] == view["legal_actions"]
    assert "最高行动目标" not in payload["play_style_guidance"]
    assert request.configuration_fingerprint != request_for(view, book=frozen, prompt_version=PLAYER_PROMPT_VERSION).configuration_fingerprint
    broken = Rulebook(tuple((n, t) for n, t in current.files if n != "roles/werewolf-witch/SKILL.md"))
    with pytest.raises(RulebookError):
        broken.select("witch", "vote")


@pytest.mark.parametrize("extras", [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
@pytest.mark.asyncio
async def test_offline_games_with_new_skills_and_constraints_settle(extras):
    from werewolf.mock_adapter import MockAdapter

    hunter, guard, knight = extras
    game = GameEngine(38, GameConfig(version="nine-player-v2-gods", players=9, wolves=3,
                                   hunters=hunter, guards=guard, knights=knight, villagers=3))
    connection = ModelConnection(id="mock", provider="mock", model="fixed", base_url="mock://local")
    agents = {seat: PlayerAgent(SeatProfile(seat=seat, connection_id="mock"), connection,
                               PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL)) for seat in range(1, 10)}
    for _ in range(300):
        if game.finished:
            break
        seat = game.next_actor()
        game.submit(seat, await agents[seat].decide(game.player_view(seat), MockAdapter()))
    assert game.finished


@pytest.mark.parametrize("win_condition", ["eliminate_side", "parity"])
@pytest.mark.parametrize("extra", ["hunter", "guard", "knight"])
def test_confirmed_factions_are_sound_through_random_real_engine_actions(win_condition, extra):
    """Hidden identities are an oracle only in the test, never helper input."""
    for seed in range(12):
        game = GameEngine(seed, GameConfig(version="nine-player-v2-gods", players=9, wolves=3, villagers=3,
                                          last_words=True, win_condition=win_condition, **{extra + "s": 1}))
        truth = {p["seat"]: p["role"] for p in game.snapshot()["state"]["players"]}
        rng = random.Random(seed)
        for _ in range(300):
            if game.finished:
                break
            seat = game.next_actor()
            view = game.player_view(seat)
            facts = decision_facts(view)
            assert all(truth[s] != "wolf" for s in facts["confirmed_good"])
            assert all(truth[s] == "wolf" for s in facts["confirmed_wolves"])
            assert all(truth[int(s)] == role for s, role in facts["known_roles"].items())
            spec = view["legal_actions"][0]
            kind = spec["kind"]
            if kind == "witch":
                potion = rng.choice(list(spec["potions"]))
                action = Action(kind, potion=potion, target=rng.choice(spec["potions"][potion]) if potion != "skip" else None)
            elif kind in {"speak", "last_words", "wolf_message"}:
                # Bluffing changes the speech, never the factual knowledge.
                action = Action(kind, text="我是村民，上一位翻牌好人！")
            else:
                targets = spec["targets"] + ([None] if spec["allow_abstain"] else [])
                action = Action(kind, target=rng.choice(targets), text="先试这个刀口" if kind == "propose_kill" else None)
            game.submit(seat, action)
        assert game.finished
