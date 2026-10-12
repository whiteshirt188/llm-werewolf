"""Immutable connection/seat profiles. Credentials stay outside model context."""

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


class FrozenProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class GenerationParams(FrozenProfile):
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_output_tokens: int = Field(default=768, ge=1)
    target_output_tokens: int = Field(default=256, ge=1)
    reasoning_budget: int | None = Field(default=None, ge=1)
    thinking_mode: Literal["disabled", "low"] = "disabled"
    thinking_output_tokens: int = Field(default=4096, ge=1024, le=16384)

    def decision_config(self) -> dict:
        # Disabled/legacy profiles keep their exact historical configuration identity.
        return self.model_dump(exclude={"thinking_mode", "thinking_output_tokens"}
                               if self.thinking_mode == "disabled" else set())


class ModelConnection(FrozenProfile):
    id: str = Field(min_length=1)
    label: str = ""
    provider: Literal["deepseek", "dashscope", "siliconflow", "mock"]
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    credential_env: str | None = None
    adapter_version: str = "adapter-contract-v1"
    generation: GenerationParams = Field(default_factory=GenerationParams)

    def decision_config(self) -> dict:
        """Effective model identity; display labels and credential references are not prompts."""
        return {
            "provider": self.provider, "model": self.model, "base_url": self.base_url,
            "generation": self.generation.decision_config(), "adapter_version": self.adapter_version,
        }

    @property
    def thinking_enabled(self) -> bool:
        return self.provider in {"deepseek", "dashscope"} and self.generation.thinking_mode == "low"


class PersonaProfile(FrozenProfile):
    label: str = "冷静分析"
    description: str = Field(default="语气冷静，结合可见证据表达判断。", max_length=2000)
    risk: Literal["cautious", "balanced", "aggressive"] = "balanced"
    bluff: Literal["low", "medium", "high"] = "medium"
    strategy: Literal["balanced", "evidence", "contradictions", "relationships", "pressure"] = "balanced"
    play_style: Literal["serious", "intuitive", "observer", "provocateur", "playful", "follower"] = "serious"

    def decision_config(self, *, include_play_style: bool = True) -> dict:
        excluded = {"label"} if include_play_style else {"label", "play_style"}
        return self.model_dump(exclude=excluded)


DEFAULT_PLAY_STYLES = ("serious", "intuitive", "observer", "provocateur", "playful", "follower", "serious", "observer")
LEGACY_PLAY_STYLE_HINTS = {
    "serious": "重视推理与取胜，会形成自己的判断，也会怀疑或改变看法；不必每轮写完整分析。",
    "intuitive": "较相信第一印象与直觉，愿意先站边再观察，直觉不需要补造证据。",
    "observer": "喜欢看局势与人际互动，选择感兴趣的争执介入，也可以有鲜明立场。",
    "provocateur": "喜欢追问、拱火与试探别人的反应，可能故意说得笃定，也会收回试探。",
    "playful": "喜欢即兴、玩笑和有趣的互动，可以临时起意或牺牲胜率，不必每轮都整活。",
    "follower": "容易被有感染力的玩家带动，会站队、维护关系，也可能因失望转向。",
}
FACTION_PLAY_STYLE_HINTS = {
    **LEGACY_PLAY_STYLE_HINTS,
    "playful": "喜欢即兴、玩笑和有趣的互动，愿意尝试出人意料的打法，仍想让自己阵营获胜；不必每轮整活。",
    "follower": "容易被有感染力的玩家带动，会站队、维护关系，也可能因失望转向；跟风不能盖过本人已知的真实查验和队友身份。",
}
PLAY_STYLE_HINTS = {
    style: hint + " 阵营获胜是最高行动目标，不能违背本人确认的真实信息。"
    for style, hint in FACTION_PLAY_STYLE_HINTS.items()
}


class SeatProfile(FrozenProfile):
    seat: int = Field(ge=1, le=9)
    nickname: str = ""
    avatar: str = ""
    connection_id: str = Field(min_length=1)
    persona: PersonaProfile = Field(default_factory=PersonaProfile)


def persona_preset(name: str) -> PersonaProfile:
    descriptions = {
        "冷静分析": "语气冷静，结合可见证据表达判断。",
        "简短直接": "用简洁直白的措辞表达重点。",
        "话痨": "表达活跃，愿意讲清自己的观点，但遵守本次字数限制。",
        "毒舌": "措辞犀利，针对游戏内发言提出质疑。",
        "戏精": "语气有戏剧感，善用悬念表达自己的立场。",
    }
    if name not in descriptions:
        raise ValueError(f"未知个性预设：{name}")
    return PersonaProfile(label=name, description=descriptions[name])
