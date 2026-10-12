"""Single-process coordinator. Each game has one worker and one commit lock."""

import asyncio
import secrets
import sqlite3
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .agent import ModelAdapter, ModelUsage, PlayerAgent, reasoning_warnings, requested_thinking_mode
from .deepseek import ProviderError
from .engine import Action, GameConfig, GameEngine, RuleViolation
from .mock_adapter import MockAdapter
from .opening import DEFAULT_OPENING_POLICY, OpeningPolicy
from .player_state import LEGACY_PROTOCOL, PLAYER_PROTOCOL, PlayerState, apply_update
from .profiles import ModelConnection, SeatProfile, content_hash
from .prompts import (
    PLAYER_PROMPT_VERSION,
    PREVIOUS_PLAYER_PROMPT_VERSION,
    PROMPT_VERSION,
    PhaseLimits,
    PromptBuilder,
)
from .recovery import DecisionRecovery, RecoveryStrategy
from .rulebook import Rulebook, RulebookError
from .storage import Store


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message
        super().__init__(message)


@dataclass
class Runtime:
    id: str
    engine: GameEngine
    human_seat: int
    token_hash: str
    book: Rulebook
    connections: list[ModelConnection]
    profiles: list[SeatProfile]
    status: str = "running"
    pause_reason: str | None = None
    turn_version: str = field(default_factory=lambda: uuid4().hex)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    reading_remaining: float = 0
    reading_deadline: float = 0
    playback_mode: str = "manual"
    step_id: str | None = None
    step_duration: float = 0
    prepared: dict | None = None
    speech_to_vote: bool = False
    mode: str = "mock"
    real_binding: dict | None = None
    simulated_transport: bool = False
    phase_limits: PhaseLimits | None = field(default_factory=PhaseLimits)
    player_protocol: str = PLAYER_PROTOCOL
    player_prompt_version: str = PLAYER_PROMPT_VERSION
    player_states: dict[int, PlayerState] = field(default_factory=dict)
    opening_policy: OpeningPolicy | None = None
    opening_seats: dict[int, str] = field(default_factory=dict)
    decision_recovery: DecisionRecovery | None = None

    @property
    def preparation_active(self) -> bool:
        return self.opening_policy is not None and any(s in {"pending", "in_flight"} for s in self.opening_seats.values())

    def reading_left(self) -> float:
        return max(0, self.reading_deadline - monotonic()) if self.reading_deadline else self.reading_remaining

    def visible(self) -> dict:
        view = self.engine.player_view(self.human_seat)
        if not view["alive"] and not self.engine.finished:
            # A dead human is a public spectator, not a continuing wolf-team observer.
            checks = view["checks"] if view["phase"] == "last_words" and view["legal_actions"] else []
            view = self.engine.player_view(self.human_seat, public_only=True)
            view["checks"] = checks
        # Skill controls belong to the human's private action panel; the shared
        # stage remains ordinary speech until a non-null duel publicly occurs.
        if view["phase"] == "knight_duel":
            view["phase"] = "day_speech"
        if self.reading_remaining or self.step_id or self.preparation_active:
            view["legal_actions"] = []
            if "current_actor" in view:
                view["current_actor"] = None
        return {
            "game_id": self.id, "mode": self.mode, "status": self.status,
            "simulated_transport": self.simulated_transport,
            "human_seat": self.human_seat, "pause_reason": self.pause_reason,
            "turn_version": self.turn_version if self.status == "running" and view["legal_actions"] and not self.reading_remaining else None,
            "reading": bool(self.reading_remaining),
            "playback_mode": self.playback_mode,
            "step_waiting": self.step_id is not None,
            "step_id": self.step_id,
            "speech_to_vote": self.speech_to_vote,
            "preparation": ({"active": self.preparation_active,
                             "completed": sum(s in {"done", "skipped"} for s in self.opening_seats.values()),
                             "total": len(self.opening_seats)} if self.opening_policy else None),
            "decision_recovery": self.decision_recovery.public()
            if self.status == "paused" and self.decision_recovery else None,
            "view": view,
            "seat_profiles": [{"seat": p.seat, "nickname": p.nickname,
                               "persona": p.persona.model_dump(), "connection_id": p.connection_id,
                               "model": next(c.model for c in self.connections if c.id == p.connection_id)}
                              for p in self.profiles],
        }

    def snapshot(self) -> dict:
        return {
            "id": self.id, "engine": self.engine.snapshot(), "human_seat": self.human_seat,
            "token_hash": self.token_hash, "rulebook_files": self.book.files,
            "connections": [c.model_dump() for c in self.connections],
            "profiles": [p.model_dump() for p in self.profiles], "status": self.status,
            "pause_reason": self.pause_reason, "turn_version": self.turn_version,
            "reading_remaining": self.reading_left(),
            "playback_mode": self.playback_mode, "step_id": self.step_id,
            "step_duration": self.step_duration, "prepared": self.prepared,
            "speech_to_vote": self.speech_to_vote,
            "mode": self.mode, "real_binding": self.real_binding,
            "simulated_transport": self.simulated_transport,
            "phase_limits": asdict(self.phase_limits) if self.phase_limits is not None else None,
            "player_protocol": self.player_protocol,
            "player_prompt_version": self.player_prompt_version,
            "player_states": {str(seat): state.model_dump() for seat, state in self.player_states.items()},
            **({"decision_recovery": self.decision_recovery.model_dump()} if self.decision_recovery else {}),
            **({"opening_policy": asdict(self.opening_policy), "opening_seats": self.opening_seats}
               if self.opening_policy else {}),
        }

    def filter_legacy_spectator(self, state: dict) -> dict:
        """Read-time filtering also protects pre-UX-1 stored streams and receipts."""
        if state["view"]["phase"] == "knight_duel":
            state = deepcopy(state)
            state["view"]["phase"] = "day_speech"
        if state["status"] == "finished" or state["view"]["alive"]:
            return state
        public_events = self.engine.player_view(self.human_seat, public_only=True)["events"]
        public_hashes = {content_hash({k: event[k] for k in ("day", "kind", "data")}) for event in public_events}
        result = deepcopy(state)
        view = result["view"]
        view["events"] = [event for event in view["events"]
                          if content_hash({k: event[k] for k in ("day", "kind", "data")}) in public_hashes]
        for index, event in enumerate(view["events"], 1):
            event["id"] = index
        shooting = view["role"] == "hunter" and view["phase"] == "hunter_shot"
        farewell = view["phase"] == "last_words" and any(a["kind"] == "last_words" for a in view["legal_actions"])
        if not farewell:
            view["checks"] = []
        view["wolf_teammates"] = []
        if not shooting and not farewell:
            view["legal_actions"] = []
        view.pop("witch", None)
        view.pop("guard", None)
        view.pop("knight", None)
        if not shooting and not farewell:
            result["turn_version"] = None
        return result


class GameService:
    def __init__(self, path: Path, adapter: ModelAdapter | None = None, timeout: float = 30,
                 reading_scale: float = 1, opening_enabled: bool = True) -> None:
        self.store = Store(path)
        self.adapter = adapter or MockAdapter()
        self.timeout = timeout
        self.reading_scale = reading_scale
        self.games: dict[str, Runtime] = {}
        self._create_lock = asyncio.Lock()
        self._closing = False
        self.real_gateway = None  # Bound after account vault initialization, never restored as a secret.
        self.retry_delay = 1
        self.opening_policy = DEFAULT_OPENING_POLICY if opening_enabled else None
        self.store.interrupt_attempts()
        for saved in self.store.load_games():
            engine = GameEngine.from_snapshot(saved["engine"])
            buffered = deepcopy(saved.get("prepared"))
            # Only translate a valid old snapshot hash through a trusted schema
            # restore. An already stale buffer must remain stale and be discarded.
            if buffered and buffered.get("engine_hash") == content_hash(saved["engine"]):
                buffered["engine_hash"] = content_hash(engine.snapshot())
            runtime = Runtime(
                id=saved["id"], engine=engine,
                human_seat=saved["human_seat"], token_hash=saved["token_hash"],
                book=Rulebook(tuple(tuple(item) for item in saved["rulebook_files"])),
                connections=[ModelConnection.model_validate(c) for c in saved["connections"]],
                profiles=[SeatProfile.model_validate(p) for p in saved["profiles"]],
                status=saved["status"], pause_reason=saved["pause_reason"],
                turn_version=saved["turn_version"],
                reading_remaining=saved.get("reading_remaining", 0),
                playback_mode=saved.get("playback_mode", "auto"),
                step_id=saved.get("step_id"), step_duration=saved.get("step_duration", 0),
                prepared=buffered,
                mode=saved.get("mode", "mock"), real_binding=saved.get("real_binding"),
                simulated_transport=saved.get("simulated_transport", False),
                phase_limits=PhaseLimits(**saved["phase_limits"]) if saved.get("phase_limits") is not None else None,
                player_protocol=saved.get("player_protocol", LEGACY_PROTOCOL),
                player_prompt_version=saved.get("player_prompt_version", PREVIOUS_PLAYER_PROMPT_VERSION
                                               if saved.get("player_protocol") == PLAYER_PROTOCOL else PROMPT_VERSION),
                player_states={int(seat): PlayerState.model_validate(state)
                               for seat, state in saved.get("player_states", {}).items()},
                opening_policy=OpeningPolicy(**saved["opening_policy"]) if saved.get("opening_policy") else None,
                opening_seats={int(seat): "skipped" if value == "in_flight" else value
                               for seat, value in saved.get("opening_seats", {}).items()},
                decision_recovery=DecisionRecovery.model_validate(saved["decision_recovery"])
                if saved.get("decision_recovery") else None,
                speech_to_vote=saved.get("speech_to_vote", bool(
                    (saved.get("step_id") or saved.get("reading_remaining"))
                    and saved["engine"]["state"]["phase"] in {"vote", "runoff_vote"})),
            )
            if runtime.status not in {"finished", "abandoned"}:
                # A pre-fix paused table also gets a meaningful recovery path.
                if runtime.pause_reason == "truncated_response" and runtime.decision_recovery is None:
                    attempts = self.store.attempts(runtime.id)
                    if attempts and attempts[-1].get("error") == "truncated_response":
                        seat = runtime.engine.next_actor()
                        profile = next((p for p in runtime.profiles if p.seat == seat), None)
                        connection = next((c for c in runtime.connections if profile and c.id == profile.connection_id), None)
                        if connection and connection.thinking_enabled and attempts[-1]["seat"] == seat:
                            runtime.decision_recovery = self._recovery(runtime, connection, seat, attempts[-1])
                runtime.status, runtime.pause_reason = "paused", "server_restarted"
                runtime.turn_version = uuid4().hex
                self.store.commit(runtime.id, runtime.snapshot(), runtime.visible())
            self.games[runtime.id] = runtime

    def authorize(self, game_id: str, token: str | None) -> Runtime:
        runtime = self.games.get(game_id)
        if runtime is None:
            raise ServiceError(404, "对局不存在")
        digest = sha256((token or "").encode()).hexdigest()
        if not secrets.compare_digest(digest, runtime.token_hash):
            raise ServiceError(403, "需要该对局的人类会话凭据")
        return runtime

    def _commit(self, runtime: Runtime, engine: GameEngine | None = None,
                status: str | None = None, pause_reason: str | None = None,
                receipt: tuple[str, str] | None = None,
                attempt: tuple[str, dict] | None = None, playback_mode: str | None = None,
                release_step: bool = False, prepared: dict | None = None,
                player_update: tuple[int, dict] | None = None,
                opening_update: tuple[int, str] | None = None, opening_intent: str | None = None,
                new_attempt: dict | None = None,
                recovery_update: tuple[DecisionRecovery | None] | None = None) -> dict:
        remaining = runtime.reading_left()
        mode = playback_mode or runtime.playback_mode
        step_id = None if release_step else runtime.step_id
        step_duration = runtime.step_duration
        buffered = prepared if prepared is not None else runtime.prepared
        vote_intro = runtime.speech_to_vote
        if engine is not None:
            previous = runtime.engine.snapshot()["state"]["events"]
            new_events = engine.snapshot()["state"]["events"][len(previous):]
            vote_intro = any(event["kind"] == "voting_started" for event in new_events)
            remaining = self._reading_delay(runtime, engine, new_events)
            buffered = None
            if mode == "manual":
                step_id = uuid4().hex if self._reading_delay(runtime, engine, new_events, scaled=False) else None
                step_duration = remaining
                remaining = 0
        elif playback_mode is not None:
            if mode == "auto":
                remaining = runtime.step_duration if runtime.step_id else remaining
                step_id = None
            elif runtime.playback_mode != "manual":
                step_id = uuid4().hex if remaining or runtime.engine.next_actor() != runtime.human_seat else None
                step_duration, remaining = remaining, 0
        if status in {"finished", "abandoned"}:
            remaining, step_id, buffered = 0, None, None
        if release_step or (not remaining and not step_id):
            vote_intro = False
        states = dict(runtime.player_states)
        if player_update is not None:
            if engine is None:
                raise ValueError("玩家状态必须与行动一起提交")
            seat, update = player_update
            states[seat] = apply_update(states.get(seat, PlayerState()), update)
        opening_seats = dict(runtime.opening_seats)
        if opening_update is not None:
            seat, value = opening_update
            opening_seats[seat] = value
            if opening_intent is not None:
                if value != "done" or engine is not None:
                    raise ValueError("开局打算只能与准备完成一起提交")
                states[seat] = apply_update(states.get(seat, PlayerState()), {"intent": opening_intent})
        candidate = Runtime(
            id=runtime.id, engine=engine or runtime.engine, human_seat=runtime.human_seat,
            token_hash=runtime.token_hash, book=runtime.book,
            connections=runtime.connections, profiles=runtime.profiles,
            status=status or runtime.status, pause_reason=pause_reason,
            reading_remaining=remaining,
            playback_mode=mode, step_id=step_id, step_duration=step_duration, prepared=buffered,
            speech_to_vote=vote_intro,
            mode=runtime.mode, real_binding=runtime.real_binding,
            simulated_transport=runtime.simulated_transport,
            phase_limits=runtime.phase_limits,
            player_protocol=runtime.player_protocol, player_prompt_version=runtime.player_prompt_version,
            player_states=states,
            opening_policy=runtime.opening_policy, opening_seats=opening_seats,
            decision_recovery=None if engine is not None or prepared is not None or status in {"finished", "abandoned"}
            else recovery_update[0] if recovery_update is not None else runtime.decision_recovery,
        )
        if candidate.status == "running" and remaining:
            candidate.reading_deadline = monotonic() + remaining
        visible = candidate.visible()
        stored_receipt = (*receipt, visible) if receipt else None
        changed = self.store.commit(runtime.id, candidate.snapshot(), visible, stored_receipt, attempt, new_attempt=new_attempt)
        # The old runtime is untouched if any SQL statement fails.
        runtime.engine, runtime.status = candidate.engine, candidate.status
        runtime.pause_reason, runtime.turn_version = candidate.pause_reason, candidate.turn_version
        runtime.reading_remaining, runtime.reading_deadline = candidate.reading_remaining, candidate.reading_deadline
        runtime.playback_mode, runtime.step_id = candidate.playback_mode, candidate.step_id
        runtime.step_duration, runtime.prepared = candidate.step_duration, candidate.prepared
        runtime.speech_to_vote = candidate.speech_to_vote
        runtime.player_states = candidate.player_states
        runtime.opening_seats = candidate.opening_seats
        runtime.decision_recovery = candidate.decision_recovery
        if changed:
            runtime.changed.set()
            runtime.changed = asyncio.Event()
        return visible

    def _reading_delay(self, runtime: Runtime, engine: GameEngine, events: list[dict], *, scaled: bool = True) -> float:
        """Presentation gate, not an engine rule. Never use unseen private text."""
        human = engine.player_view(runtime.human_seat)
        seconds = 0.0
        for event in events:
            if event["audience"] is None:
                if event["kind"] in {"speak", "last_words"}:
                    # Conservative allowance covers the slowest UI speed plus SSE/poll and reading slack.
                    seconds += len(event["data"]["text"]) * 0.260 + 3
                elif event["kind"] in {"night_started", "dawn", "day_started", "voting_started", "eliminated", "runoff_started", "no_elimination", "hunter_ready", "hunter_shot", "knight_duel", "last_words_started"}:
                    seconds += 3
            elif human["alive"] and human["role"] == "wolf" and runtime.human_seat in event["audience"]:
                if event["kind"] == "wolf_proposals":
                    seconds += sum((len(f"提议刀 {item['target']} 号：{item['text']}") * 0.260 + 3)
                                   for seat, item in event["data"]["proposals"].items() if int(seat) != runtime.human_seat)
                elif event["kind"] == "wolf_replies":
                    seconds += sum(len(text) * 0.260 + 3 for seat, text in event["data"]["replies"].items()
                                   if int(seat) != runtime.human_seat)
                elif event["kind"] == "wolf_message" and event["data"]["seat"] != runtime.human_seat:
                    seconds += len(event["data"]["text"]) * 0.260 + 3
        return seconds * (self.reading_scale if scaled else 1)

    async def create(self, seed: int | None, human_seat: int, connections: list[ModelConnection],
                     profiles: list[SeatProfile], playback_mode: str = "manual",
                     config: GameConfig | None = None) -> tuple[dict, str]:
        async with self._create_lock:
            if any(g.status in {"running", "paused"} for g in self.games.values()):
                raise ServiceError(409, "已有进行中或暂停的对局")
            if any(c.provider != "mock" for c in connections):
                raise ServiceError(422, "本阶段只开放显式模拟模式")
            try:
                book = Rulebook.load()
            except RulebookError as exc:
                raise ServiceError(503, "规则资料不可用，暂不能创建对局") from exc
            token = secrets.token_urlsafe(32)
            runtime = Runtime(id=uuid4().hex, engine=GameEngine(seed if seed is not None else secrets.randbits(64), config,
                                                             wolf_reply_mode="sequential"),
                              human_seat=human_seat, token_hash=sha256(token.encode()).hexdigest(),
                              book=book, connections=connections, profiles=profiles,
                              playback_mode=playback_mode, opening_policy=self.opening_policy,
                              opening_seats={p.seat: "pending" for p in profiles} if self.opening_policy else {})
            if playback_mode == "manual":
                runtime.step_id, runtime.step_duration = uuid4().hex, 3 * self.reading_scale
            try:
                self.store.commit(runtime.id, runtime.snapshot(), runtime.visible())
            except sqlite3.IntegrityError as exc:
                raise ServiceError(409, "已有进行中对局，请使用单进程服务") from exc
            self.games[runtime.id] = runtime
            visible = runtime.visible()
            self._schedule(runtime)
            return visible, token

    async def create_real(self, user_id, session_token, plan, operation_id, expected_versions, confirm_cost):
        if not confirm_cost:
            raise ServiceError(422, "必须明确确认真实对局的供应商调用及费用，包括最多一步预取")
        if self.real_gateway is None:
            raise ServiceError(503, "真实模型服务不可用")
        request_hash = content_hash({"plan": plan.model_dump(), "expected_versions": expected_versions})
        async with self._create_lock:
            previous = self.store.game_start(user_id, operation_id)
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ServiceError(409, "开局操作编号已用于不同配置")
                runtime = self.games[previous["game_id"]]
                return runtime.visible(), self.real_gateway.decrypt_token(previous["encrypted_token"])
            if any(g.status in {"running", "paused"} for g in self.games.values()):
                raise ServiceError(409, "已有进行中或暂停的对局，请恢复或结束原局")
            # Short metadata-only check. No await between consistent admission and durable creation.
            try:
                connections, profiles, book, binding = self.real_gateway.prepare(user_id, session_token, plan, expected_versions)
            except RulebookError:
                raise ServiceError(503, "规则资料不可用，暂不能创建对局") from None
            except ProviderError:
                raise ServiceError(403, "开局账号授权已失效，请重新登录") from None
            token = secrets.token_urlsafe(32)
            runtime = Runtime(id=uuid4().hex, engine=GameEngine(plan.seed if plan.seed is not None else secrets.randbits(64),
                                                             plan.roster.engine_config(), wolf_reply_mode="batch"
                                                             if binding.get("qualification_required") else "sequential"),
                              human_seat=plan.human_seat, token_hash=sha256(token.encode()).hexdigest(),
                              book=book, connections=connections, profiles=profiles,
                              playback_mode=plan.playback_mode, mode="real", real_binding=binding,
                              player_protocol=LEGACY_PROTOCOL if binding.get("qualification_required") else PLAYER_PROTOCOL,
                              player_prompt_version=PROMPT_VERSION if binding.get("qualification_required") else PLAYER_PROMPT_VERSION,
                              opening_policy=None if binding.get("qualification_required") else self.opening_policy,
                              opening_seats={p.seat: "pending" for p in profiles}
                              if self.opening_policy and not binding.get("qualification_required") else {},
                              simulated_transport=bool(getattr(self.real_gateway.factory, "simulated", False)))
            if plan.playback_mode == "manual":
                runtime.step_id, runtime.step_duration = uuid4().hex, 3 * self.reading_scale
            self.store.commit(runtime.id, runtime.snapshot(), runtime.visible(), start=(
                user_id, operation_id, request_hash, self.real_gateway.encrypt_token(token)
            ))
            self.games[runtime.id] = runtime
            self._schedule(runtime)
            return runtime.visible(), token

    async def pause_owner(self, user_id):
        for runtime in self.games.values():
            if runtime.real_binding and runtime.real_binding["owner_id"] == user_id and runtime.status in {"running", "paused"}:
                async with runtime.lock:
                    self._commit(runtime, status="paused", pause_reason="account_session_expired")
                    if runtime.worker and not runtime.worker.done():
                        runtime.worker.cancel()

    async def rebind_real(self, runtime, user_id, session_token):
        async with runtime.lock:
            binding = self.real_gateway.rebind_session(runtime, user_id, session_token)
            original = runtime.real_binding
            runtime.real_binding = binding
            try:
                self._commit(runtime, status=runtime.status, pause_reason="user_paused")
            except Exception:
                runtime.real_binding = original
                raise

    def _schedule(self, runtime: Runtime) -> None:
        if (not self._closing and runtime.status == "running"
                and (runtime.preparation_active or runtime.reading_remaining or runtime.engine.next_actor() != runtime.human_seat)
                and (runtime.worker is None or runtime.worker.done())):
            runtime.worker = asyncio.create_task(self._drive(runtime))

    def _duplicate(self, runtime: Runtime, operation_id: str, payload: dict) -> dict | None:
        previous = self.store.receipt(runtime.id, operation_id)
        if previous:
            if previous[0] != content_hash(payload):
                raise ServiceError(409, "同一操作 ID 不可用于不同请求")
            return runtime.filter_legacy_spectator(previous[1])
        return None

    async def submit(self, runtime: Runtime, operation_id: str, expected_version: str, action: Action) -> dict:
        wire_action = asdict(action)
        if action.potion is None:
            wire_action.pop("potion")  # Preserve hashes for pre-witch idempotency receipts.
        payload = {"operation": "action", "expected_version": expected_version, "action": wire_action}
        async with runtime.lock:
            duplicate = self._duplicate(runtime, operation_id, payload)
            if duplicate is not None:
                return duplicate
            if runtime.status != "running":
                raise ServiceError(409, "对局当前不可行动")
            if runtime.preparation_active:
                raise ServiceError(409, "玩家还在拿牌准备，请稍候")
            if runtime.reading_remaining or runtime.step_id:
                raise ServiceError(409, "请等上一位玩家说完并读完本轮消息")
            if expected_version != runtime.turn_version:
                raise ServiceError(409, "行动版本已过期，请刷新玩家视图")
            if runtime.engine.next_actor() != runtime.human_seat:
                raise ServiceError(409, "当前不是人类玩家的回合")
            engine = GameEngine.from_snapshot(runtime.engine.snapshot())
            try:
                engine.submit(runtime.human_seat, action)
            except RuleViolation as exc:
                raise ServiceError(422, str(exc)) from exc
            result = self._commit(runtime, engine, "finished" if engine.finished else "running",
                                  receipt=(operation_id, content_hash(payload)))
            self._schedule(runtime)
            return result

    async def advance(self, runtime: Runtime, operation_id: str, expected_step: str) -> dict:
        payload = {"operation": "advance", "expected_step": expected_step}
        async with runtime.lock:
            duplicate = self._duplicate(runtime, operation_id, payload)
            if duplicate is not None:
                return duplicate
            if runtime.status != "running" or runtime.playback_mode != "manual":
                raise ServiceError(409, "当前不可单步继续")
            if runtime.preparation_active:
                raise ServiceError(409, "玩家还在拿牌准备，请稍候")
            if runtime.step_id is None or runtime.step_id != expected_step:
                raise ServiceError(409, "该展示步骤已过期，请同步当前牌桌")
            result = self._commit(runtime, release_step=True, receipt=(operation_id, content_hash(payload)))
            self._schedule(runtime)
            return result

    async def set_playback(self, runtime: Runtime, operation_id: str, mode: str) -> dict:
        payload = {"operation": "playback", "mode": mode}
        async with runtime.lock:
            duplicate = self._duplicate(runtime, operation_id, payload)
            if duplicate is not None:
                return duplicate
            if runtime.status in {"finished", "abandoned"}:
                raise ServiceError(409, "对局已经结束")
            result = self._commit(runtime, playback_mode=mode, receipt=(operation_id, content_hash(payload)))
            self._schedule(runtime)
            return result

    async def control(self, runtime: Runtime, operation_id: str, operation: str,
                      recovery: RecoveryStrategy | None = None) -> dict:
        payload = {"operation": operation, **({"recovery": recovery} if recovery else {})}
        async with runtime.lock:
            duplicate = self._duplicate(runtime, operation_id, payload)
            if duplicate is not None:
                return duplicate
            if runtime.status in {"finished", "abandoned"}:
                raise ServiceError(409, "对局已经结束")
            if operation in {"pause", "abandon"}:
                result = self._commit(runtime, status="abandoned" if operation == "abandon" else "paused",
                                      pause_reason="user_abandoned" if operation == "abandon" else "user_paused",
                                      receipt=(operation_id, content_hash(payload)))
                worker = runtime.worker
                if worker and not worker.done():
                    worker.cancel()
            else:
                if runtime.worker is not None and not runtime.worker.done():
                    raise ServiceError(409, "上一决策正在取消，请稍后继续")
                selection = runtime.decision_recovery
                if selection and (selection.engine_hash != content_hash(runtime.engine.snapshot())
                                  or selection.seat != runtime.engine.next_actor()
                                  or selection.state_revision != runtime.player_states.get(selection.seat, PlayerState()).revision):
                    raise ServiceError(409, "恢复信息与当前局面不一致，请刷新局面")
                if recovery:
                    if runtime.status != "paused" or selection is None:
                        raise ServiceError(409, "当前没有需要更换请求方式的截断行动")
                    if recovery == "more_tokens" and selection.increased_limit is None:
                        raise ServiceError(409, "本次额度已达上限，可选择本次关闭思考继续")
                    selection = selection.model_copy(update={"strategy": recovery})
                elif selection and selection.strategy is None:
                    raise ServiceError(409, "上次请求已截断，请选择本次增加额度或关闭思考，避免重复消耗")
                if runtime.real_binding:
                    try:
                        self.real_gateway.check_session(runtime.real_binding)
                    except ProviderError:
                        self._commit(runtime, status="paused", pause_reason="account_session_expired")
                        raise ServiceError(403, "请用开局账号登录并恢复真实对局授权") from None
                result = self._commit(runtime, status="running", recovery_update=(selection,),
                                      receipt=(operation_id, content_hash(payload)))
                self._schedule(runtime)
            return result

    @staticmethod
    def _recovery(runtime, connection, seat, record) -> DecisionRecovery:
        return DecisionRecovery(engine_hash=content_hash(runtime.engine.snapshot()), seat=seat,
            state_revision=runtime.player_states.get(seat, PlayerState()).revision,
            base_limit=connection.generation.thinking_output_tokens,
            output_limit=record.get("output_limit") or connection.generation.thinking_output_tokens,
            output_tokens=record.get("usage", {}).get("output_tokens"),
            reasoning_tokens=record.get("usage", {}).get("reasoning_tokens"),
            thinking_mode=record.get("thinking_mode"))

    async def _decision(self, runtime, agent, view, engine_hash, state_revision=0, *, opening=False,
                        frozen_connection=None, recovery_strategy=None):
        """Each retry is independently durable; all attempts use one frozen agent/request view."""
        retries = 1 if opening or recovery_strategy else 3 if runtime.mode == "real" else 1
        thinking = agent.connection.thinking_enabled
        multiplier = agent.prepare(view).timeout_multiplier if not opening else None
        timeout = self.timeout * (multiplier or (2 if thinking else 1))
        for retry in range(retries):
            async with runtime.lock:
                if (runtime.status != "running" or self._closing or content_hash(runtime.engine.snapshot()) != engine_hash
                        or runtime.player_states.get(agent.profile.seat, PlayerState()).revision != state_revision):
                    return None, None, "cancelled"
                try:
                    adapter = self.real_gateway.adapter(runtime, frozen_connection or agent.connection) if runtime.mode == "real" else self.adapter
                except ProviderError as exc:
                    return None, None, exc.code
                except (ServiceError, ValueError):
                    return None, None, "connection_unavailable"
                except Exception:  # noqa: BLE001 -- no outbound request has been made
                    return None, None, "internal_error"
                pending = {"attempt_id": uuid4().hex, "seat": agent.profile.seat,
                           "provider": agent.connection.provider, "model": agent.connection.model,
                           "purpose": "opening_preparation" if opening else "game",
                           "action_kind": "opening_plan" if opening else view["legal_actions"][0]["kind"],
                           "connection_id": agent.connection.id, "retry": retry, "call_count": 1,
                           "thinking_mode": requested_thinking_mode(agent.connection), "timeout_seconds": timeout,
                           "status": "in_flight", "usage": asdict(ModelUsage()),
                           "simulated": runtime.mode == "mock" or bool(getattr(adapter, "simulated", False)),
                           "action": None, "error": None}
                if recovery_strategy:
                    pending["recovery_strategy"] = recovery_strategy
                attempt_id = pending["attempt_id"]
                if opening:
                    self._commit(runtime, opening_update=(agent.profile.seat, "in_flight"), new_attempt=pending)
                else:
                    self.store.start_attempt(attempt_id, runtime.id, pending)
            action, error, retryable = None, None, False
            before = len(agent.records)
            try:
                async with asyncio.timeout(timeout) as deadline:
                    action = await (agent.prepare_opening(view, adapter) if opening else agent.decide(view, adapter))
                if deadline.expired():
                    # A transport may finish after swallowing cancellation; its usage is still retained.
                    action, error = None, "timeout"
                    retryable = not thinking and not opening
                    pending["error_type"] = "TimeoutError"
            except asyncio.CancelledError:
                error = "cancelled"
            except ProviderError as exc:
                error, retryable = exc.code, exc.retryable and not (thinking and exc.code in {"timeout", "truncated_response"})
                pending["error_type"] = "ProviderError"
                pending["usage"] = asdict(exc.usage)
            except TimeoutError:
                error, retryable = "timeout", not thinking
                pending["error_type"] = "TimeoutError"
            except RuleViolation:
                error, retryable = "invalid_action", True
                pending["error_type"] = "InvalidModelResponse"
            except Exception as exc:  # noqa: BLE001 -- retain only a safe class, never provider exception text
                error = "internal_error"
                pending["error_type"] = type(exc).__name__
            record = {**pending, **asdict(agent.records[-1])} if len(agent.records) > before else dict(pending)
            # Provider errors may carry billed usage despite having no ModelResponse.
            if any(v is not None for v in pending["usage"].values()):
                record["usage"] = pending["usage"]
            record["warnings"] = tuple(dict.fromkeys((*record.get("warnings", ()), *reasoning_warnings(
                record.get("reasoning_budget"), record["usage"].get("reasoning_tokens"),
                thinking_mode=record.get("thinking_mode")))))
            record["simulated"] = pending["simulated"] or record["simulated"]
            record["error"] = error
            if error == "cancelled":
                record["status"] = "cancelled"
            elif error:
                record["status"] = "error"
            attempt = (attempt_id, record)
            if action is not None or not retryable or retry == retries - 1:
                return action, attempt, error
            async with runtime.lock:
                self._commit(runtime, status=runtime.status, pause_reason=runtime.pause_reason, attempt=attempt)
                if runtime.status != "running" or self._closing:
                    return None, None, "cancelled"
            await asyncio.sleep(self.retry_delay * (retry + 1))
        raise AssertionError("unreachable")

    async def _prepare_players(self, runtime: Runtime) -> None:
        semaphore = asyncio.Semaphore(runtime.opening_policy.concurrency)

        async def prepare(seat):
            async with semaphore:
                async with runtime.lock:
                    if runtime.status != "running" or self._closing or runtime.opening_seats[seat] != "pending":
                        return
                    profile = next(p for p in runtime.profiles if p.seat == seat)
                    connection = next(c for c in runtime.connections if c.id == profile.connection_id)
                    state = runtime.player_states.get(seat, PlayerState())
                    agent = PlayerAgent(profile, connection, PromptBuilder(runtime.book, phase_limits=runtime.phase_limits,
                        protocol=runtime.player_protocol, player_state=state, prompt_version=runtime.player_prompt_version,
                        opening_policy=runtime.opening_policy))
                    view, engine_hash = runtime.engine.player_view(seat), content_hash(runtime.engine.snapshot())
                intent, attempt, error = await self._decision(runtime, agent, view, engine_hash, state.revision, opening=True)
                async with runtime.lock:
                    if attempt is None and error == "cancelled":
                        return
                    valid = (runtime.status == "running" and not self._closing
                             and content_hash(runtime.engine.snapshot()) == engine_hash
                             and runtime.player_states.get(seat, PlayerState()).revision == state.revision)
                    if not valid and attempt and error != "cancelled":
                        attempt[1]["status"] = "discarded"
                    self._commit(runtime, status=runtime.status, pause_reason=runtime.pause_reason, attempt=attempt,
                                 opening_update=(seat, "done" if valid and intent is not None else "skipped"),
                                 opening_intent=intent if valid else None)

        async with asyncio.TaskGroup() as group:
            for seat, status in runtime.opening_seats.items():
                if status == "pending":
                    group.create_task(prepare(seat))

    async def _drive(self, runtime: Runtime) -> None:
        try:
            if runtime.preparation_active:
                await self._prepare_players(runtime)
            while True:
                async with runtime.lock:
                    if runtime.status != "running" or self._closing:
                        return
                    waiting = runtime.reading_left() if runtime.reading_remaining else 0
                    signal = runtime.changed
                    if runtime.reading_remaining and waiting <= 0:
                        # Gate release is durable and invalidates stale actions; no attempt is created.
                        self._commit(runtime)
                if waiting > 0:
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=min(waiting, 30))
                    except TimeoutError:
                        pass
                    continue
                async with runtime.lock:
                    if runtime.status != "running" or self._closing:
                        return
                    if runtime.prepared is not None:
                        if runtime.step_id:
                            return
                        buffered = runtime.prepared
                        if buffered["engine_hash"] != content_hash(runtime.engine.snapshot()):
                            raise ValueError("prepared action has a stale engine state")
                        if buffered.get("state_revision", 0) != runtime.player_states.get(buffered["seat"], PlayerState()).revision:
                            raise ValueError("prepared action has a stale player state")
                        engine = GameEngine.from_snapshot(runtime.engine.snapshot())
                        engine.submit(buffered["seat"], Action.from_dict(buffered["action"]))
                        update = buffered.get("player_update")
                        self._commit(runtime, engine, "finished" if engine.finished else "running",
                                     player_update=(buffered["seat"], update) if update is not None else None)
                        continue
                    seat = runtime.engine.next_actor()
                    if seat is None or seat == runtime.human_seat:
                        return
                    profile = next(p for p in runtime.profiles if p.seat == seat)
                    connection = next(c for c in runtime.connections if c.id == profile.connection_id)
                    state = runtime.player_states.get(seat, PlayerState())
                    recovery = runtime.decision_recovery
                    if recovery and (recovery.engine_hash != content_hash(runtime.engine.snapshot()) or recovery.seat != seat
                                     or recovery.state_revision != state.revision):
                        raise ValueError("decision recovery has a stale state")
                    effective_connection = recovery.connection(connection) if recovery else connection
                    agent = PlayerAgent(profile, effective_connection, PromptBuilder(runtime.book, phase_limits=runtime.phase_limits,
                                        protocol=runtime.player_protocol, player_state=state,
                                        prompt_version=runtime.player_prompt_version, opening_policy=runtime.opening_policy,
                                        output_override=recovery.increased_limit if recovery and recovery.strategy == "more_tokens" else None))
                    view = runtime.engine.player_view(seat)
                    engine_hash = content_hash(runtime.engine.snapshot())
                action, attempt, error = await self._decision(runtime, agent, view, engine_hash, state.revision,
                    frozen_connection=connection, recovery_strategy=recovery.strategy if recovery else None)
                async with runtime.lock:
                    if (runtime.status != "running" or content_hash(runtime.engine.snapshot()) != engine_hash or self._closing
                            or runtime.player_states.get(seat, PlayerState()).revision != state.revision):
                        if attempt and error != "cancelled":
                            attempt[1]["status"] = "discarded"
                        self._commit(runtime, status=runtime.status, pause_reason=runtime.pause_reason,
                                     attempt=attempt)
                        return
                    if action is None:
                        selection = (self._recovery(runtime, connection, seat, attempt[1])
                                     if connection.thinking_enabled and error == "truncated_response" and attempt
                                     else recovery.model_copy(update={"strategy": None}) if recovery else None)
                        self._commit(runtime, status="paused", pause_reason=error if runtime.mode == "real" else "mock_decision_failed",
                                     attempt=attempt, recovery_update=(selection,))
                        return
                    update = attempt[1].get("player_update") if attempt else None
                    if runtime.step_id or runtime.reading_remaining:
                        # Persist billed usage immediately, but do not settle or publish the prepared decision.
                        self._commit(runtime, prepared={"seat": seat, "action": asdict(action),
                                                       "engine_hash": engine_hash, "state_revision": state.revision,
                                                       "player_update": update}, attempt=attempt)
                        if runtime.step_id:
                            return
                        continue
                    engine = GameEngine.from_snapshot(runtime.engine.snapshot())
                    engine.submit(seat, action)
                    self._commit(runtime, engine, "finished" if engine.finished else "running",
                                 attempt=attempt, player_update=(seat, update) if update is not None else None)
        except (sqlite3.Error, RuleViolation, ValueError, ExceptionGroup) as exc:
            async with runtime.lock:
                runtime.status, runtime.pause_reason = "paused", "service_error"
                runtime.turn_version = uuid4().hex
                try:
                    self.store.commit(runtime.id, runtime.snapshot(), runtime.visible())
                except sqlite3.Error:
                    # Memory is paused even if storage is unavailable; durable state remains the last commit.
                    runtime.pause_reason = "storage_unavailable"
                runtime.changed.set()
                runtime.changed = asyncio.Event()
                # Expose only a safe diagnostic class to trusted server code.
                runtime.failure_type = type(exc).__name__
        finally:
            runtime.worker = None

    async def events(self, runtime: Runtime, after: int) -> tuple[list[dict], int, asyncio.Event]:
        async with runtime.lock:
            cursor = self.store.cursor(runtime.id)
            if after > cursor:
                raise ServiceError(409, "事件游标超出当前范围")
            packets = self.store.events(runtime.id, after)
            for packet in packets:
                packet["state"] = runtime.filter_legacy_spectator(packet["state"])
            return packets, cursor, runtime.changed

    async def close(self) -> None:
        self._closing = True
        workers = []
        for runtime in self.games.values():
            async with runtime.lock:
                if runtime.status == "running":
                    self._commit(runtime, status="paused", pause_reason="server_stopped")
                if runtime.worker and not runtime.worker.done():
                    workers.append(runtime.worker)
                    runtime.worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.store.close()
