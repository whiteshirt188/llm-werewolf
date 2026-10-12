"""Bounded public roster settings; no arbitrary powers or engine-state input."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .engine import GameConfig


class RosterConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    players: int = Field(default=6, ge=6, le=9)
    wolves: int = Field(default=2, ge=1, le=4)
    seers: int = Field(default=1, ge=1, le=1)
    witches: int = Field(default=1, ge=0, le=1)
    hunters: int = Field(default=0, ge=0, le=1)
    guards: int = Field(default=0, ge=0, le=1)
    knights: int = Field(default=0, ge=0, le=1)
    last_words: bool = True

    @model_validator(mode="after")
    def valid_composition(self):
        if self.wolves >= self.players - self.wolves:
            raise ValueError("阵容中狼人必须少于好人")
        if self.villagers < 1:
            raise ValueError("至少保留一名村民")
        gods = self.seers + self.witches + self.hunters + self.guards + self.knights
        if (self.players > 6 or self.hunters or self.guards or self.knights) and gods != 3:
            raise ValueError("预言家必选，并从女巫、猎人、守卫、骑士中选择另外两名神职")
        return self

    @property
    def villagers(self):
        return self.players - self.wolves - self.seers - self.witches - self.hunters - self.guards - self.knights

    def engine_config(self) -> GameConfig:
        values = self.model_dump()
        version = "custom-player-v2-gods" if self.hunters or self.guards or self.knights else "custom-player-v1"
        if self == RosterConfig():
            version = "six-player-v3-witch"
        elif self.players == 9 and self.wolves == 3:
            version = "nine-player-v2-gods"
        return GameConfig(version=version, villagers=self.villagers, **values)
