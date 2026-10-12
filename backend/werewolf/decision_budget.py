"""Versioned v10 budget policy, selected from the true role and engine round."""

from dataclasses import dataclass

from .profiles import ModelConnection


@dataclass(frozen=True)
class DecisionBudgetPolicy:
    version: str = "round-role-v1"
    later_from_day: int = 2
    later_multiplier: int = 2
    god_multiplier: int = 2
    gods: tuple[str, ...] = ("seer", "witch", "hunter", "guard", "knight")
    thinking_ceiling: int = 16384
    reasoning_ceiling: int = 32768
    large_thinking_threshold: int = 8192
    large_timeout_multiplier: int = 4

    def multiplier(self, day: int, role: str) -> int:
        return (self.later_multiplier if day >= self.later_from_day else 1) * (
            self.god_multiplier if role in self.gods else 1)

    def thinking_limit(self, connection: ModelConnection, day: int, role: str) -> int:
        return min(connection.generation.thinking_output_tokens * self.multiplier(day, role), self.thinking_ceiling)

    def preview(self, connection: ModelConnection) -> dict | None:
        if not connection.thinking_enabled:
            return None
        return {"opening": connection.generation.thinking_output_tokens,
                "first_regular": self.thinking_limit(connection, 1, "villager"),
                "first_god": self.thinking_limit(connection, 1, "seer"),
                "later_regular": self.thinking_limit(connection, 2, "villager"),
                "later_god": self.thinking_limit(connection, 2, "seer")}


V10_BUDGET_POLICY = DecisionBudgetPolicy()
