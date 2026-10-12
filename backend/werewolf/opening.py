"""Frozen, one-call-per-seat opening preparation policy."""

from dataclasses import dataclass

OPENING_PROTOCOL = "opening-intent-v1"


@dataclass(frozen=True)
class OpeningPolicy:
    version: str = OPENING_PROTOCOL
    concurrency: int = 3
    output_limit: int = 256

    def __post_init__(self):
        if self.version != OPENING_PROTOCOL or self.concurrency != 3 or self.output_limit != 256:
            raise ValueError("未知开局准备配置")


DEFAULT_OPENING_POLICY = OpeningPolicy()
