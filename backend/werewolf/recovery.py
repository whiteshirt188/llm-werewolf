"""Explicit, single-decision recovery; never changes a table's frozen connection."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .deepseek import ADAPTER_VERSION
from .profiles import ModelConnection
from .qwen import ADAPTER_VERSION as QWEN_ADAPTER_VERSION

RecoveryStrategy = Literal["more_tokens", "without_thinking"]
MAX_RECOVERY_TOKENS = 16384


class DecisionRecovery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_hash: str
    seat: int
    state_revision: int
    base_limit: int
    output_limit: int
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    thinking_mode: str | None = None
    strategy: RecoveryStrategy | None = None

    @property
    def increased_limit(self) -> int | None:
        current = max(self.base_limit, self.output_limit)
        return min(current * 2, MAX_RECOVERY_TOKENS) if current < MAX_RECOVERY_TOKENS else None

    def public(self) -> dict:
        # No seat, action, role, private memory or engine revision in the UI.
        return {"output_limit": self.output_limit, "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens, "thinking_mode": self.thinking_mode,
                "increased_limit": self.increased_limit}

    def connection(self, frozen: ModelConnection) -> ModelConnection:
        generation = frozen.generation
        if self.strategy == "without_thinking":
            return frozen.model_copy(update={
                "generation": generation.model_copy(update={"thinking_mode": "disabled"}),
                "adapter_version": QWEN_ADAPTER_VERSION if frozen.provider == "dashscope" else ADAPTER_VERSION})
        if self.strategy == "more_tokens":
            if self.increased_limit is None:
                raise ValueError("恢复额度已经达到上限")
            return frozen.model_copy(update={"generation": generation.model_copy(
                update={"thinking_output_tokens": self.increased_limit})})
        return frozen
