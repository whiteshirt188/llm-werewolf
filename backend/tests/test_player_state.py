"""Scripted behavior continuity and durable private-state boundaries; no live models."""

import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest
from app_helpers import flow_app as create_app
from test_api import opened
from test_manual import advance, prepared
from test_qualification import environment
from test_real_games import GameDouble, start_body

from werewolf.agent import InvalidModelResponse, ModelResponse, PlayerAgent
from werewolf.api import CreateGame
from werewolf.demo import simulated_action
from werewolf.engine import Action, GameEngine
from werewolf.mock_adapter import MockAdapter
from werewolf.player_state import LEGACY_PROTOCOL, PLAYER_PROTOCOL, PlayerState, apply_update
from werewolf.profiles import (
    DEFAULT_PLAY_STYLES,
    ModelConnection,
    PersonaProfile,
    SeatProfile,
    canonical_json,
    content_hash,
)
from werewolf.prompts import (
    FACTION_PLAYER_PROMPT_VERSION,
    PREVIOUS_PLAYER_PROMPT_VERSION,
    ContextPolicy,
    PromptBuilder,
)
from werewolf.rulebook import Rulebook


def until(engine, kind, seat=None, role=None):
    for _ in range(200):
        actor = engine.next_actor()
        view = engine.player_view(actor)
        if (view["legal_actions"][0]["kind"] == kind and (seat is None or actor == seat)
                and (role is None or view["role"] == role)):
            return view
        engine.submit(actor, simulated_action(view))
    raise AssertionError("script did not reach requested action")


def agent_for(view, state=None, style="serious", protocol=PLAYER_PROTOCOL, policy=None):
    connection = ModelConnection(id="shared", provider="mock", model="fixed", base_url="mock://local")
    profile = SeatProfile(seat=view["seat"], connection_id=connection.id, persona=PersonaProfile(play_style=style))
    return PlayerAgent(profile, connection, PromptBuilder(Rulebook.load(), policy, protocol=protocol, player_state=state))


class Script:
    def __init__(self, action, update):
        self.action, self.update, self.calls = action, update, []

    async def complete(self, request):
        self.calls.append(request)
        return ModelResponse(canonical_json({"action": self.action, "player_update": self.update}), simulated=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("style,text,stance", [
    ("serious", "我先怀疑2号，想听你回应刚才的问题。", "先怀疑2号"),
    ("intuitive", "我跳预言家，2号查杀。就是想试试你的反应。", "先压2号看看"),
    ("provocateur", "2号你急什么？我先站你对面，等会再说。", "先和2号较劲"),
])
async def test_scripted_player_keeps_stance_and_performance_then_can_change_mind(style, text, stance):
    engine = GameEngine(38)
    view = until(engine, "speak", role="villager")
    seat = view["seat"]
    before = engine.snapshot()
    update = {"attitudes": [{"seat": 2, "attitude": "suspect"}], "claim": "seer", "stance": stance,
              "intent": "试探一下2号"}
    agent = agent_for(view, style=style)
    script = Script({"kind": "speak", "text": text}, update)
    action = await agent.decide(view, script)
    assert engine.snapshot() == before  # A claim never grants a role or changes real checks.
    assert agent.records[-1].player_update_error is None
    state = apply_update(PlayerState(), agent.records[-1].player_update)
    engine.submit(seat, action)
    vote = until(engine, "vote", seat=seat)
    assert vote["role"] == "villager" and vote["checks"] == []
    voter = agent_for(vote, state, style)
    request = voter.prepare(vote)
    payload = json.loads(request.messages[-1].content)
    assert payload["player_state"]["claim"] == "seer" and payload["player_state"]["stance"] == stance
    assert payload["persona"]["play_style"] == style
    assert any(e["data"].get("text") == text for e in payload["history"])
    target = vote["legal_actions"][0]["targets"][-1]
    changed = Script({"kind": "vote", "target": target}, {"claim": None, "stance": "我改主意了", "intent": None})
    action = await voter.decide(vote, changed)
    state = apply_update(state, voter.records[-1].player_update)
    engine.submit(seat, action)
    assert state.claim is None and state.stance == "我改主意了" and state.intent is None
    assert len(script.calls) == len(changed.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    None, [], {"role": "seer"}, {"checks": [{"target": 2, "is_wolf": True}]}, {"intent": "长" * 41},
    {"reply_to": 99999}, {"reply_to": True},
    {"attitudes": [{"seat": 9, "attitude": "suspect"}]},
    {"attitudes": [{"seat": 2, "attitude": "suspect", "evidence": [99999]}]},
    {"attitudes": [{"seat": 2, "attitude": "trust"}, {"seat": 2, "attitude": "suspect"}]},
    {"attitudes": [{"seat": s, "attitude": "suspect"} for s in (1, 2, 3)]},
])
async def test_invalid_memory_does_not_reject_valid_action_or_add_calls(update):
    engine = GameEngine(38)
    view = until(engine, "speak", role="villager")
    agent = agent_for(view)
    script = Script({"kind": "speak", "text": "我想先听2号回应。"}, update)
    action = await agent.decide(view, script)
    engine.submit(view["seat"], action)
    assert len(script.calls) == 1 and agent.records[-1].status == "ok"
    assert agent.records[-1].player_update is None
    assert agent.records[-1].player_update_error == "invalid_player_update"


@pytest.mark.asyncio
async def test_empty_update_is_valid_and_invalid_action_does_not_publish_update():
    engine = GameEngine(38)
    view = until(engine, "vote")
    agent = agent_for(view)
    await agent.decide(view, Script({"kind": "vote", "target": None}, {}))
    assert agent.records[-1].player_update == {}
    assert apply_update(PlayerState(), {}) == PlayerState()
    with pytest.raises(InvalidModelResponse):
        await agent.decide(view, Script({"kind": "vote", "target": view["seat"]}, {"intent": "不能保存我"}))
    assert agent.records[-1].player_update is None and agent.records[-1].status == "invalid_response"


@pytest.mark.asyncio
async def test_duplicate_update_keys_discard_only_memory_while_duplicate_actions_stay_invalid():
    class Raw:
        def __init__(self, content):
            self.content = content

        async def complete(self, request):
            return ModelResponse(self.content, simulated=True)

    view = until(GameEngine(38), "speak", role="villager")
    agent = agent_for(view)
    action = await agent.decide(view, Raw('{"action":{"kind":"speak","text":"我先听2号"},'
                                         '"player_update":{"intent":"先说","intent":"再说"}}'))
    assert action.text == "我先听2号" and agent.records[-1].player_update_error == "invalid_player_update"
    with pytest.raises(InvalidModelResponse):
        await agent.decide(view, Raw('{"action":{"kind":"speak","text":"先说","text":"再说"},"player_update":{}}'))


@pytest.mark.asyncio
async def test_visible_references_and_private_state_survive_history_pruning():
    engine = GameEngine(38)
    view = until(engine, "vote")
    own_event = next(e for e in view["events"] if e["kind"] == "speak" and e["data"]["seat"] != view["seat"])
    target = own_event["data"]["seat"]
    update = {"attitudes": [{"seat": target, "attitude": "trust", "evidence": [own_event["id"]]}],
              "reply_to": own_event["id"], "intent": "接他刚才的话"}
    agent = agent_for(view)
    await agent.decide(view, Script({"kind": "vote", "target": None}, update))
    state = apply_update(PlayerState(), agent.records[-1].player_update)
    pruned = agent_for(view, state, policy=ContextPolicy(soft_char_limit=1)).prepare(view)
    payload = json.loads(pruned.messages[-1].content)
    assert not payload["history"] and payload["player_state"]["reply_to"] == own_event["id"]
    assert payload["interaction"]["reply_event"] == own_event
    assert state.attitudes[0].evidence == [own_event["id"]]


class RememberingMock(MockAdapter):
    def __init__(self):
        self.calls = []

    async def complete(self, request):
        self.calls.append(request)
        response = await super().complete(request)
        data = json.loads(response.content)
        if request.protocol == PLAYER_PROTOCOL and len(self.calls) == 1:
            data["player_update"] = {"claim": "seer", "intent": "私密记忆标记", "stance": "暂时站2号",
                                     "attitudes": [{"seat": 2, "attitude": "trust"}]}
        return ModelResponse(canonical_json(data), simulated=True)


@pytest.mark.asyncio
async def test_prefetch_state_is_private_and_only_applied_once_after_restart_and_release(tmp_path):
    path, adapter = tmp_path / "state.sqlite3", RememberingMock()
    app = create_app(path, adapter, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        gid, token = created["game"]["game_id"], created["session_token"]
        await prepared(app, gid)
        runtime = app.state.service.games[gid]
        buffered = deepcopy(runtime.prepared)
        seat = buffered["seat"]
        assert not runtime.player_states and buffered["player_update"]["intent"] == "私密记忆标记"
        for suffix in ("", "/events", "/stream?follow=false", "/usage"):
            response = await client.get(f"/api/games/{gid}{suffix}")
            assert "私密记忆标记" not in response.text and "player_states" not in response.text
            assert "player_update" not in response.text
        assert len(adapter.calls) == len(app.state.service.store.attempts(gid)) == 1
        await client.post(f"/api/games/{gid}/pause", json={"operation_id": "pause"})
    restarted = create_app(path, adapter, reading_scale=0)
    async with opened(restarted) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        runtime = restarted.state.service.games[gid]
        assert runtime.prepared == buffered and not runtime.player_states
        await client.post(f"/api/games/{gid}/resume", json={"operation_id": "resume"})
        await asyncio.sleep(.01)
        assert len(adapter.calls) == 1
        visible = (await client.get(f"/api/games/{gid}")).json()
        assert (await advance(client, visible, "release")).status_code == 200
        async with asyncio.timeout(2):
            while not runtime.visible()["turn_version"]:
                current = runtime.visible()
                if current["step_id"]:
                    await advance(client, current, f"step-{current['step_id']}")
                await asyncio.sleep(.001)
        assert runtime.player_states[seat].revision == 1 and runtime.player_states[seat].claim == "seer"
        assert runtime.player_states[seat].intent == "私密记忆标记"
        for request in adapter.calls[1:]:
            payload = json.loads(request.messages[-1].content)
            if request.seat == seat:
                assert payload["player_state"]["intent"] == "私密记忆标记"
            else:
                assert "私密记忆标记" not in request.messages[-1].content
        assert sum(r.seat == seat and json.loads(r.messages[-1].content)["state"]["legal_actions"][0]["kind"] == "propose_kill"
                   for r in adapter.calls) == 1
        await client.post(f"/api/games/{gid}/pause", json={"operation_id": "pause-again"})
    restored = create_app(path, adapter, reading_scale=0)
    async with opened(restored):
        assert restored.state.service.games[gid].player_states[seat].intent == "私密记忆标记"


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_action_and_memory_together(tmp_path):
    app = create_app(tmp_path / "rollback.sqlite3", RememberingMock(), reading_scale=0)
    async with opened(app) as client:
        state = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()["game"]
        gid = state["game_id"]
        await prepared(app, gid)
        service, runtime = app.state.service, app.state.service.games[gid]
        before = runtime.snapshot()
        buffered = runtime.prepared
        engine = GameEngine.from_snapshot(runtime.engine.snapshot())
        engine.submit(buffered["seat"], Action.from_dict(buffered["action"]))
        with pytest.raises(ValueError, match="事务回滚"):
            service._commit(runtime, engine, attempt=("nonexistent", {"status": "ok"}),
                            player_update=(buffered["seat"], buffered["player_update"]))
        assert runtime.snapshot() == before
        assert content_hash(service.store.load_games()[0]) == content_hash(before)


@pytest.mark.asyncio
async def test_cancelled_call_returning_late_does_not_apply_action_or_memory(tmp_path):
    class Late(RememberingMock):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def complete(self, request):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().complete(request)

    adapter = Late()
    app = create_app(tmp_path / "late.sqlite3", adapter, reading_scale=0)
    async with opened(app) as client:
        visible = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()["game"]
        gid = visible["game_id"]
        runtime = app.state.service.games[gid]
        before = runtime.engine.snapshot()
        await asyncio.wait_for(adapter.started.wait(), 2)
        await client.post(f"/api/games/{gid}/pause", json={"operation_id": "pause"})
        async with asyncio.timeout(2):
            while runtime.worker is not None:
                await asyncio.sleep(.001)
        assert runtime.engine.snapshot() == before and not runtime.player_states and runtime.prepared is None
        assert app.state.service.store.attempts(gid)[0]["status"] == "discarded"


@pytest.mark.asyncio
async def test_real_protocol_bad_update_is_recorded_without_paid_retry(tmp_path):
    class BadMemory(GameDouble):
        async def complete(self, request):
            response = await super().complete(request)
            data = json.loads(response.content)
            data["player_update"] = {"reply_to": 999999}
            return ModelResponse(canonical_json(data), response.usage, simulated=True)

    game = BadMemory()
    async with environment(tmp_path, qualification_enabled=False, game_factory=game, reading_scale=0) as (app, client, _, headers, cid, _):
        body = await start_body(client, headers, cid)
        changed = deepcopy(body)
        changed["plan"]["ai_profiles"][0]["persona"]["play_style"] = "playful"
        rejected = await client.post("/api/account/games", headers=headers, json=changed)
        assert rejected.status_code == 409 and not game.calls
        result = await client.post("/api/account/games", headers=headers, json=body)
        assert result.status_code == 201
        gid = result.json()["game"]["game_id"]
        await prepared(app, gid)
        attempts = app.state.service.store.attempts(gid)
        assert len(attempts) == len(game.calls) == 1
        assert attempts[0]["status"] == "ok" and attempts[0]["player_update_error"] == "invalid_player_update"
        assert attempts[0]["usage"]["input_tokens"] == 100
        assert app.state.service.games[gid].prepared["player_update"] is None


def test_new_profiles_are_mixed_and_legacy_fingerprints_are_exact():
    profiles = CreateGame(human_seat=4).ai_profiles
    assert [p.persona.play_style for p in profiles] == list(DEFAULT_PLAY_STYLES[:5])
    view = GameEngine(38).player_view(1)
    legacy = agent_for(view, protocol=LEGACY_PROTOCOL).prepare(view)
    payload = json.loads(legacy.messages[-1].content)
    assert "player_state" not in payload and "play_style" not in payload["persona"]
    connection = legacy.connection
    expected = {"connection": connection.decision_config(), "persona": payload["persona"], "rules": view["rules"],
                "rulebook": legacy.rulebook_fingerprint, "prompt": "player-agent-v5-role-objectives-channels",
                "context": "two-days-v1", "policy": {"recent_days": 2, "soft_char_limit": 16000},
                "phase_limits": {"night_reasoning": 512, "day_reasoning": 1024, "night_output": 512, "day_output": 768}}
    assert legacy.configuration_fingerprint == content_hash(expected)
    serious = agent_for(view).prepare(view)
    playful = agent_for(view, style="playful").prepare(view)
    assert serious.configuration_fingerprint != playful.configuration_fingerprint != legacy.configuration_fingerprint
    assert serious.max_output_tokens == playful.max_output_tokens == legacy.max_output_tokens
    assert agent_for(view, style="playful", protocol=LEGACY_PROTOCOL).prepare(view).wire_messages() == legacy.wire_messages()


@pytest.mark.asyncio
async def test_legacy_restore_uses_frozen_protocol_and_does_not_recall_prepared_action(tmp_path):
    path, adapter = tmp_path / "legacy.sqlite3", RememberingMock()
    app = create_app(path, adapter, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        gid, token = created["game"]["game_id"], created["session_token"]
        await prepared(app, gid)
        runtime = app.state.service.games[gid]
        frozen = runtime.book.files
        await client.post(f"/api/games/{gid}/pause", json={"operation_id": "pause"})
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT snapshot FROM games WHERE id=?", (gid,)).fetchone()[0])
        saved.pop("player_protocol")
        saved.pop("player_prompt_version")
        saved.pop("player_states")
        saved["prepared"].pop("player_update")
        saved["prepared"].pop("state_revision")
        for profile in saved["profiles"]:
            profile["persona"].pop("play_style")
        db.execute("UPDATE games SET snapshot=? WHERE id=?", (canonical_json(saved), gid))
    restarted = create_app(path, adapter, reading_scale=0)
    async with opened(restarted) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        runtime = restarted.state.service.games[gid]
        assert runtime.player_protocol == LEGACY_PROTOCOL and runtime.book.files == frozen
        await client.post(f"/api/games/{gid}/resume", json={"operation_id": "resume"})
        visible = (await client.get(f"/api/games/{gid}")).json()
        await advance(client, visible, "next")
        async with asyncio.timeout(2):
            while not runtime.visible()["turn_version"]:
                current = runtime.visible()
                if current["step_id"]:
                    await advance(client, current, f"step-{current['step_id']}")
                await asyncio.sleep(.001)
        assert len(adapter.calls) > 1 and all(r.protocol == LEGACY_PROTOCOL for r in adapter.calls[1:])
        assert not runtime.player_states
        assert len([a for a in restarted.state.service.store.attempts(gid) if a["seat"] == 1 and a["action_kind"] == "propose_kill"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_version", [3, 4, 5])
async def test_previous_player_prompt_and_paid_buffer_are_frozen_across_restore_and_commit(tmp_path, snapshot_version):
    path, adapter = tmp_path / "previous-prompt.sqlite3", RememberingMock()
    app = create_app(path, adapter, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        await client.post(f"/api/games/{gid}/pause", json={"operation_id": "pause"})
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT snapshot FROM games WHERE id=?", (gid,)).fetchone()[0])
        saved.pop("player_prompt_version")  # v6 snapshots predate the explicit prompt version.
        saved["engine"]["snapshot_version"] = snapshot_version
        saved["engine"]["config"].pop("win_condition")
        if snapshot_version < 5:
            for key in ("after_last_words", "last_words_shot_candidates"):
                saved["engine"]["state"].pop(key)
        saved["prepared"]["engine_hash"] = content_hash(saved["engine"])
        db.execute("UPDATE games SET snapshot=? WHERE id=?", (canonical_json(saved), gid))
    restarted = create_app(path, adapter, reading_scale=0)
    async with opened(restarted):
        runtime = restarted.state.service.games[gid]
        assert runtime.player_prompt_version == PREVIOUS_PLAYER_PROMPT_VERSION
        assert runtime.prepared["engine_hash"] == content_hash(runtime.engine.snapshot())
        expected_buffer = {**saved["prepared"], "engine_hash": content_hash(runtime.engine.snapshot())}
        seat = runtime.engine.next_actor()
        profile = next(p for p in runtime.profiles if p.seat == seat)
        connection = next(c for c in runtime.connections if c.id == profile.connection_id)
        profile = profile.model_copy(update={"persona": PersonaProfile(play_style="playful")})
        view = runtime.engine.player_view(seat)
        before = PromptBuilder(runtime.book, protocol=runtime.player_protocol,
                               prompt_version=runtime.player_prompt_version).build(profile, connection, view)
        payload = json.loads(before.messages[-1].content)
        assert "牺牲胜率" in payload["play_style_guidance"] and "取胜不是唯一乐趣" in payload["communication"]["objective"]
        current = PromptBuilder(runtime.book, protocol=PLAYER_PROTOCOL).build(profile, connection, view)
        assert current.configuration_fingerprint != before.configuration_fingerprint
        restarted.state.service._commit(runtime, status="paused", pause_reason="user_paused")
        persisted = restarted.state.service.store.load_games()[0]
        assert persisted["player_prompt_version"] == PREVIOUS_PLAYER_PROMPT_VERSION
        assert persisted["prepared"] == expected_buffer and len(adapter.calls) == 1
        buffered = runtime.prepared
        engine = GameEngine.from_snapshot(runtime.engine.snapshot())
        engine.submit(buffered["seat"], Action.from_dict(buffered["action"]))
        restarted.state.service._commit(runtime, engine, status="paused", pause_reason="user_paused",
                                        player_update=(buffered["seat"], buffered["player_update"]))
        persisted = restarted.state.service.store.load_games()[0]
        assert persisted["engine"]["snapshot_version"] == (5 if snapshot_version == 5 else 6)
        assert "win_condition" not in persisted["engine"]["config"]
        assert runtime.player_states[buffered["seat"]].revision == 1 and len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_v7_role_documents_prompt_and_prepared_update_survive_v8_restore(tmp_path):
    path, adapter = tmp_path / "v7-frozen.sqlite3", RememberingMock()
    app = create_app(path, adapter, reading_scale=0)
    async with opened(app) as client:
        created = (await client.post("/api/games", json={"seed": 38, "human_seat": 4})).json()
        gid = created["game"]["game_id"]
        await prepared(app, gid)
        await client.post(f"/api/games/{gid}/pause", json={"operation_id": "pause"})
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT snapshot FROM games WHERE id=?", (gid,)).fetchone()[0])
        old_paths = {f"roles/werewolf-{role}/SKILL.md": f"references/roles/{role}.md"
                     for role in ("wolf", "seer", "witch", "hunter", "guard", "knight", "villager")}
        saved["rulebook_files"] = [[old_paths.get(name, name), text] for name, text in Rulebook.load(legacy=True).files]
        saved["player_prompt_version"] = FACTION_PLAYER_PROMPT_VERSION
        db.execute("UPDATE games SET snapshot=? WHERE id=?", (canonical_json(saved), gid))
    restarted = create_app(path, adapter, reading_scale=0)
    async with opened(restarted):
        runtime = restarted.state.service.games[gid]
        assert runtime.player_prompt_version == FACTION_PLAYER_PROMPT_VERSION
        assert runtime.book.files == tuple(tuple(f) for f in saved["rulebook_files"])
        assert runtime.prepared == saved["prepared"] and len(adapter.calls) == 1
        buffered = runtime.prepared
        engine = GameEngine.from_snapshot(runtime.engine.snapshot())
        engine.submit(buffered["seat"], Action.from_dict(buffered["action"]))
        restarted.state.service._commit(runtime, engine, status="paused", pause_reason="user_paused",
                                        player_update=(buffered["seat"], buffered["player_update"]))
        persisted = restarted.state.service.store.load_games()[0]
        assert persisted["player_prompt_version"] == FACTION_PLAYER_PROMPT_VERSION
        assert persisted["rulebook_files"] == saved["rulebook_files"]
        assert runtime.player_states[buffered["seat"]].revision == 1 and len(adapter.calls) == 1
