"""Bounded, seat-private impressions. Never an authority for roles or game facts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LEGACY_PROTOCOL = "action-v1"
PLAYER_PROTOCOL = "player-state-v1"
PROTOCOLS = {LEGACY_PROTOCOL, PLAYER_PROTOCOL}
Claim = Literal["wolf", "seer", "witch", "villager", "hunter", "guard", "knight"]


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Attitude(PrivateModel):
    seat: int = Field(ge=1, le=9)
    attitude: Literal["trust", "suspect", "uncertain"]
    evidence: list[int] = Field(default_factory=list, max_length=2)


class PlayerUpdate(PrivateModel):
    attitudes: list[Attitude] = Field(default_factory=list, max_length=2)
    stance: str | None = Field(default=None, max_length=40)
    claim: Claim | None = None
    intent: str | None = Field(default=None, max_length=40)
    reply_to: int | None = None


class PlayerState(PrivateModel):
    revision: int = Field(default=0, ge=0)
    attitudes: list[Attitude] = Field(default_factory=list, max_length=8)
    stance: str | None = Field(default=None, max_length=40)
    claim: Claim | None = None
    intent: str | None = Field(default=None, max_length=40)
    reply_to: int | None = None


def validate_update(data: object, seat: int, players: set[int], events: set[int]) -> dict:
    update = PlayerUpdate.model_validate(data)
    targets = [item.seat for item in update.attitudes]
    if len(set(targets)) != len(targets) or any(s == seat or s not in players for s in targets):
        raise ValueError("invalid_attitude_target")
    references = [ref for item in update.attitudes for ref in item.evidence]
    if update.reply_to is not None:
        references.append(update.reply_to)
    if any(ref not in events for ref in references):
        raise ValueError("invisible_event_reference")
    return update.model_dump(exclude_unset=True)


def apply_update(state: PlayerState, data: dict) -> PlayerState:
    update = PlayerUpdate.model_validate(data).model_dump(exclude_unset=True)
    merged = state.model_dump()
    attitudes = {item["seat"]: item for item in merged["attitudes"]}
    for item in update.pop("attitudes", []):
        attitudes[item["seat"]] = item
    merged.update(update)
    merged["attitudes"] = [attitudes[seat] for seat in sorted(attitudes)]
    candidate = PlayerState.model_validate(merged)
    if candidate == state:
        return state
    return candidate.model_copy(update={"revision": state.revision + 1})


def update_schema() -> dict:
    # Fields are sparse: a model can spend its entire output allowance on the action.
    schema = PlayerUpdate.model_json_schema()
    schema.pop("title", None)
    return schema
