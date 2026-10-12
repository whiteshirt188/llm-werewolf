"""One seat, one immutable configuration. No engine or credential access."""

import asyncio
import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol

from .engine import Action, RuleViolation, validate_action
from .opening import OPENING_PROTOCOL
from .player_state import PLAYER_PROTOCOL, validate_update
from .profiles import ModelConnection, SeatProfile
from .prompts import DecisionRequest, PromptBuilder, action_schema


class InvalidModelResponse(RuleViolation):
    pass


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    simulated: bool = False


class ModelAdapter(Protocol):
    async def complete(self, request: DecisionRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class DecisionRecord:
    seat: int
    provider: str
    model: str
    configuration_fingerprint: str
    input_characters: int
    output_limit: int
    warnings: tuple[str, ...]
    latency_ms: float
    status: str
    usage: ModelUsage
    simulated: bool
    action: Action | None
    reasoning_phase: str | None = None
    reasoning_budget: int | None = None
    thinking_mode: str | None = None
    player_update: dict | None = None
    player_update_error: str | None = None


def requested_thinking_mode(connection: ModelConnection) -> str | None:
    if connection.thinking_enabled:
        return "requested_low"
    return "requested_disabled" if connection.provider in {"deepseek", "siliconflow", "dashscope"} else None


def reasoning_warnings(budget: int | None, tokens: int | None, *, thinking_mode: str | None = "requested_disabled") -> tuple[str, ...]:
    if tokens is None or tokens == 0:
        return ()
    warnings = ["thinking_disable_not_confirmed"] if thinking_mode == "requested_disabled" else []
    if budget is not None and tokens > budget:
        warnings.append("reasoning_budget_exceeded")
    return tuple(warnings)


class DuplicateObject(dict):
    """Mark ambiguous objects so a bad optional update need not invalidate the action."""


def has_duplicates(value: object) -> bool:
    if isinstance(value, dict):
        return isinstance(value, DuplicateObject) or any(has_duplicates(v) for v in value.values())
    if isinstance(value, list):
        return any(has_duplicates(v) for v in value)
    return False


def parse_json(content: str, *, mark_duplicates: bool = False) -> object:
    def unique_keys(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                if not mark_duplicates:
                    raise InvalidModelResponse("JSON 包含重复字段")
                result = DuplicateObject(result)
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise InvalidModelResponse(f"JSON 包含非法常量：{value}")

    try:
        if not isinstance(content, str) or len(content) > 16_000:
            raise InvalidModelResponse("响应必须是有限长度的 JSON 文本")
        return json.loads(content, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except (TypeError, ValueError) as exc:
        raise InvalidModelResponse(str(exc)) from exc


def parse_action(content: str, spec: dict) -> Action:
    try:
        data = parse_json(content)
        schema = action_schema(spec)
        if not isinstance(data, dict) or set(data) != set(schema["required"]):
            raise InvalidModelResponse("响应必须恰好包含本次要求的字段")
        action = Action.from_dict(data)
        validate_action(action, spec)
        return action
    except (TypeError, ValueError) as exc:
        raise InvalidModelResponse(str(exc)) from exc


def parse_decision(content: str, request: DecisionRequest, state: dict) -> tuple[Action, dict | None, str | None]:
    spec = state["legal_actions"][0]
    if request.protocol != PLAYER_PROTOCOL:
        return parse_action(content, spec), None, None
    data = parse_json(content, mark_duplicates=True)
    if not isinstance(data, dict) or isinstance(data, DuplicateObject) or set(data) != {"action", "player_update"}:
        raise InvalidModelResponse("响应必须包含 action 和 player_update")
    if has_duplicates(data["action"]):
        raise InvalidModelResponse("行动包含重复字段")
    action = parse_action(json.dumps(data["action"], ensure_ascii=False, allow_nan=False), spec)
    try:
        if has_duplicates(data["player_update"]):
            raise ValueError("duplicate_update_fields")
        update = validate_update(data["player_update"], request.seat,
                                 {p["seat"] for p in state["players"]}, set(request.visible_event_ids))
    except (TypeError, ValueError):
        # Never expose raw model text or validation messages; a valid action still settles.
        return action, None, "invalid_player_update"
    return action, update, None


class PlayerAgent:
    def __init__(self, profile: SeatProfile, connection: ModelConnection, builder: PromptBuilder) -> None:
        if profile.connection_id != connection.id:
            raise ValueError("席位引用不存在的连接")
        self._profile, self._connection, self._builder = profile, connection, builder
        self._records: list[DecisionRecord] = []
        self._busy = False

    @property
    def profile(self) -> SeatProfile:
        return self._profile

    @property
    def connection(self) -> ModelConnection:
        return self._connection

    @property
    def records(self) -> tuple[DecisionRecord, ...]:
        return tuple(self._records)

    def prepare(self, view: dict) -> DecisionRequest:
        return self._builder.build(self._profile, self._connection, view)

    async def prepare_opening(self, view: dict, adapter: ModelAdapter) -> str:
        if self._busy:
            raise RuleViolation("同一席位不能并发准备")
        request = self._builder.build_opening(self._profile, self._connection, view)
        self._busy = True
        start, response, intent, status = perf_counter(), None, None, "adapter_error"
        try:
            response = await adapter.complete(request)
            status = "invalid_response"
            data = parse_json(response.content)
            if (request.protocol != OPENING_PROTOCOL or not isinstance(data, dict) or set(data) != {"intent"}
                    or not isinstance(data["intent"], str) or not data["intent"].strip() or len(data["intent"]) > 40):
                raise InvalidModelResponse("开局准备必须是一条40字内的打算")
            intent, status = data["intent"], "ok"
            return intent
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self._records.append(DecisionRecord(
                seat=request.seat, provider=request.connection.provider, model=request.connection.model,
                configuration_fingerprint=request.configuration_fingerprint, input_characters=request.input_characters,
                output_limit=request.max_output_tokens, warnings=reasoning_warnings(request.reasoning_budget,
                    response.usage.reasoning_tokens if response else None,
                    thinking_mode=requested_thinking_mode(request.connection)), latency_ms=(perf_counter() - start) * 1000,
                status=status, usage=response.usage if response else ModelUsage(),
                simulated=response.simulated if response else False, action=None,
                reasoning_phase=request.reasoning_phase, reasoning_budget=request.reasoning_budget,
                thinking_mode=requested_thinking_mode(request.connection),
                player_update={"intent": intent} if intent is not None else None))
            self._busy = False

    async def decide(self, view: dict, adapter: ModelAdapter) -> Action:
        if self._busy:
            raise RuleViolation("同一席位不能并发决策")
        request = self.prepare(view)
        # Snapshot the legal action from the actual request, not a mutable caller view.
        state = json.loads(request.messages[-1].content)["state"]
        self._busy = True
        start = perf_counter()
        response, action = None, None
        update, update_error = None, None
        status = "adapter_error"
        try:
            response = await adapter.complete(request)
            status = "invalid_response"
            action, update, update_error = parse_decision(response.content, request, state)
            status = "ok"
            return action
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self._records.append(DecisionRecord(
                seat=request.seat, provider=request.connection.provider, model=request.connection.model,
                configuration_fingerprint=request.configuration_fingerprint,
                input_characters=request.input_characters, output_limit=request.max_output_tokens,
                warnings=request.warnings + reasoning_warnings(request.reasoning_budget, response.usage.reasoning_tokens if response else None,
                                                               thinking_mode=requested_thinking_mode(request.connection)),
                latency_ms=(perf_counter() - start) * 1000,
                status=status, usage=response.usage if response else ModelUsage(),
                simulated=response.simulated if response else False, action=action,
                reasoning_phase=request.reasoning_phase, reasoning_budget=request.reasoning_budget,
                thinking_mode=requested_thinking_mode(request.connection),
                player_update=update, player_update_error=update_error,
            ))
            self._busy = False
