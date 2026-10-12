"""Last words are real, bounded, durable turns; no network or provider keys."""

import asyncio
import json
from dataclasses import replace

import pytest
from app_helpers import flow_app as create_app
from test_api import opened, ready, submit_body
from test_gods import configured, night, seat_of

from werewolf.agent import parse_action
from werewolf.engine import Action, GameEngine, RuleViolation
from werewolf.profiles import ModelConnection, PersonaProfile, SeatProfile
from werewolf.prompts import PromptBuilder
from werewolf.qualification_questions import questions
from werewolf.roster import RosterConfig
from werewolf.rulebook import Rulebook


def game():
    return GameEngine(38, replace(configured("witch", "hunter"), last_words=True))


def banish(g, target):
    while g._state["phase"] == "day_speech":
        g.submit(g.next_actor(), Action("speak", text="讨论"))
    while g._state["phase"] == "vote":
        seat = g.next_actor()
        g.submit(seat, Action("vote", target=target if seat != target else None))


def restored(g):
    return GameEngine.from_snapshot(json.loads(json.dumps(g.snapshot())))


def test_first_night_two_deaths_have_ordered_words_but_poisoned_hunter_has_no_shot():
    g = game()
    hunter, villager = seat_of(g, "hunter"), seat_of(g, "villager")
    night(g, villager, potion="poison", poison=hunter)
    assert g._state["phase"] == "last_words"
    for seat in sorted([hunter, villager]):
        assert g.next_actor() == seat
        clone = restored(g)
        for engine in (g, clone):
            engine.submit(seat, Action("last_words", text=""))
        assert g.snapshot() == clone.snapshot()
    assert g._state["phase"] == "day_speech"
    assert not any(e["kind"] == "hunter_ready" for e in g._state["events"])


def test_banishment_hunter_words_then_shot_then_victory():
    g = game()
    night(g, seat_of(g, "villager"), potion="save")
    hunter = seat_of(g, "hunter")
    # Keep only the final wolf, so the hunter's shot determines victory.
    for wolf in g._living("wolf")[1:]:
        g._player(wolf)["alive"] = False
    g._phase("day_speech", g._living())
    banish(g, hunter)
    assert not g.finished and g.legal_actions(hunter)[0]["kind"] == "last_words"
    before = g.snapshot()
    for action in (Action("vote", target=g._living("wolf")[0]), Action("last_words", text="x" * 301), Action("last_words", text="a", target=1)):
        with pytest.raises(RuleViolation):
            g.submit(hunter, action)
        assert g.snapshot() == before
    clone = restored(g)
    for engine in (g, clone):
        engine.submit(hunter, Action("last_words", text="请关注我的判断。"))
        assert engine.legal_actions(hunter)[0]["kind"] == "shoot"
        engine.submit(hunter, Action("shoot", target=engine._living("wolf")[0]))
        assert engine.finished and engine.replay()["winner"] == "good"
        assert [e["kind"] for e in engine._state["events"]][-3:] == ["hunter_ready", "hunter_shot", "game_over"]
    assert g.snapshot() == clone.snapshot()


def test_last_wolf_gets_words_before_reveal_and_legacy_snapshots_keep_old_rules():
    g = game()
    night(g, seat_of(g, "villager"), potion="save")
    wolf = g._living("wolf")[0]
    for other in g._living("wolf")[1:]:
        g._player(other)["alive"] = False
    g._phase("day_speech", g._living())
    banish(g, wolf)
    assert not g.finished and "revealed_roles" not in g.player_view(1)
    g.submit(wolf, Action("last_words", text=""))
    assert g.finished and not g.legal_actions(wolf)
    old = GameEngine(38).snapshot()
    old["snapshot_version"] = 4
    old["config"].pop("last_words")
    old["state"].pop("after_last_words")
    old["state"].pop("last_words_shot_candidates")
    assert not GameEngine.from_snapshot(old).config.last_words


def test_subsequent_night_death_has_no_words():
    g = game()
    night(g, seat_of(g, "villager"), potion="save")
    g._next_night()  # Trusted fixture moves to a second night with no day death.
    victim = seat_of(g, "villager")
    night(g, victim)
    assert not g._player(victim)["alive"] and not g.legal_actions(victim)
    assert not any(e["kind"] == "last_words_started" for e in g._state["events"])


def test_prompt_words_bounds_strategies_and_public_constraints():
    g = game()
    seer = seat_of(g, "seer")
    night(g, seer)
    builder = PromptBuilder(Rulebook.load())
    connection = ModelConnection(id="m", provider="mock", model="fixed", base_url="mock://local")
    requests = []
    for style in ("balanced", "evidence", "contradictions", "relationships", "pressure"):
        p = SeatProfile(seat=seer, connection_id="m", persona=PersonaProfile(strategy=style))
        request = builder.build(p, connection, g.player_view(seer))
        data = json.loads(request.messages[-1].content)
        assert data["persona"]["strategy"] == style
        assert data["state"]["checks"]
        assert data["decision_context"]["settlement_pending"]
        assert data["decision_context"]["max_possible_living_wolves"] is None
        assert all(text in request.messages[0].content for _, text in Rulebook.load().select("seer", "last_words", data["state"]["rules"]))
        assert "人类" not in json.dumps(data["state"])
        assert parse_action('{"kind":"last_words","text":""}', data["state"]["legal_actions"][0]).text == ""
        requests.append(request)
    assert len({r.configuration_fingerprint for r in requests}) == 5
    g.submit(seer, Action("last_words", text="旧查验是我的信息。"))
    # A side-elimination game can continue with three wolves and two good players.
    v = g.player_view(g.next_actor())
    v["players"] = [{"seat": s, "alive": True} for s in range(1, 6)]
    p = SeatProfile(seat=v["seat"], connection_id="m")
    data = json.loads(builder.build(p, connection, v).messages[-1].content)
    assert data["decision_context"]["max_possible_living_wolves"] == 3
    assert any(e["kind"] == "last_words" for e in data["history"])
    assert not any(e["kind"] == "last_words" for e in data["facts"])


def test_qualification_checks_last_words_and_repeated_claims_without_extra_calls():
    suite = questions(RosterConfig().engine_config().__dict__)
    assert len(suite) == 12
    q = next(q for q in suite if q.id == "r4")
    assert not q.view["alive"] and q.view["legal_actions"][0]["kind"] == "last_words"
    q = next(q for q in suite if q.id == "l5")
    assert len(q.view["events"]) == 3 and q.expected_target is None


@pytest.mark.asyncio
async def test_dead_human_seer_can_read_old_checks_submit_once_and_refresh(tmp_path):
    app = create_app(tmp_path / "words.sqlite3", default_playback="auto", reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 3})).json()
        gid = created["game"]["game_id"]
        runtime = app.state.service.games[gid]
        # Test-only fixture avoids a background actor mutating the controlled setup.
        if runtime.worker:
            runtime.worker.cancel()
            await asyncio.gather(runtime.worker, return_exceptions=True)
        g = GameEngine(38, RosterConfig().engine_config())
        runtime.engine = g
        while g.player_view(1)["phase"] == "night":
            actor = g.next_actor()
            kind = g.legal_actions(actor)[0]["kind"]
            g.submit(actor, Action(kind, target=3, text="统一" if kind == "propose_kill" else None)
                     if kind in {"kill", "propose_kill"} else Action("inspect", target=1)
                     if kind == "inspect" else Action("witch", potion="skip"))
        visible = runtime.visible()
        assert visible["view"]["role"] == "seer" and not visible["view"]["alive"]
        assert visible["view"]["checks"][0]["target"] == 1
        assert not any(e["kind"] in {"inspection", "wolf_proposals", "wolf_replies", "wolf_decision"} for e in visible["view"]["events"])
        runtime.prepared = None
        app.state.service.store.commit(gid, runtime.snapshot(), visible)
        state = await ready(client, gid)
        assert state["view"]["legal_actions"][0]["kind"] == "last_words"
        assert state["view"]["checks"]
        body = submit_body(state)
        body["action"] = {"kind": "last_words", "text": "我是预言家，1号查杀。"}
        first = await client.post(f"/api/games/{gid}/actions", json=body)
        second = await client.post(f"/api/games/{gid}/actions", json=body)
        assert first.status_code == second.status_code == 200
        assert len([e for e in runtime.engine._state["events"] if e["kind"] == "last_words" and e["data"]["seat"] == 3]) == 1
        current = (await client.get(f"/api/games/{gid}")).json()
        if current["status"] != "finished":
            assert not current["view"]["checks"] and not current["view"]["legal_actions"]
