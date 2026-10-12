"""Account-scoped seat drafts and non-consuming admission preflight. Not a game."""

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .decision_budget import V10_BUDGET_POLICY
from .deepseek import ProviderError
from .opening import DEFAULT_OPENING_POLICY
from .player_state import PLAYER_PROTOCOL
from .profiles import ModelConnection, PersonaProfile, content_hash
from .prompts import PromptBuilder
from .providers import adapter_version, validate_connection
from .qualification import QualificationService
from .roster import RosterConfig
from .rulebook import Rulebook
from .service import ServiceError


class PlanSeat(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    seat: int = Field(ge=1, le=9)
    nickname: str = Field(default="", max_length=32)
    connection_id: str = Field(min_length=1, max_length=128)
    persona: PersonaProfile = Field(default_factory=PersonaProfile)

    @field_validator("persona")
    @classmethod
    def bounded_label(cls, persona):
        if len(persona.label) > 64:
            raise ValueError("个性名称过长")
        return persona


class TablePlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    roster: RosterConfig = Field(default_factory=RosterConfig)
    human_seat: int = Field(default=1, ge=1, le=9)
    seed: int | None = Field(default=None, ge=-(2**53 - 1), le=2**53 - 1)
    playback_mode: Literal["manual", "auto"] = "manual"
    ai_profiles: list[PlanSeat] = Field(min_length=5, max_length=8)

    @model_validator(mode="after")
    def unique_ai_seats(self):
        if (self.human_seat > self.roster.players or len(self.ai_profiles) != self.roster.players - 1
                or {p.seat for p in self.ai_profiles} != set(range(1, self.roster.players + 1)) - {self.human_seat}):
            raise ValueError("必须为每个非人类席位配置一个不同的 AI")
        return self


def admission_preflight(service: QualificationService, user_id: str, plan: TablePlan) -> dict:
    if not service.enabled:
        return configuration_preflight(service.vault, user_id, plan,
                                       opening_policy=getattr(service, "opening_policy", DEFAULT_OPENING_POLICY))
    # A consistent vault snapshot: no connection edit can interleave with this check.
    # There is no credential decryption, adapter construction or decision request here.
    seats, checked = [], {}
    with service.vault.lock:
        for profile in sorted(plan.ai_profiles, key=lambda p: p.seat):
            key = (profile.connection_id, content_hash(profile.persona.decision_config()))
            if key not in checked:
                try:
                    config, _ = service.vault.connection_snapshot(user_id, profile.connection_id)
                    preview = service.preview(user_id, profile.connection_id, profile.persona, plan.roster)
                    report = preview["latest_report"]
                    if preview["unsupported"]:
                        status, error = "unsupported", preview["unsupported"]
                    elif preview["cached"]:
                        status, error = "qualified", None
                    elif report:
                        status, error = report["status"], report["error"]
                        if status == "passed":
                            status = "simulated" if report["simulated"] else "not_qualified"
                    else:
                        status, error = "not_tested", None
                    checked[key] = {
                        "connection_label": config["label"], "provider": config["provider"],
                        "model": config["model"], "qualified": status == "qualified", "status": status,
                        "error": error, "report_id": report["id"] if report else None,
                        "configuration_version": preview["configuration_version"],
                    }
                except (ServiceError, ValueError):
                    # Foreign, deleted and unavailable IDs deliberately have one indistinguishable result.
                    checked[key] = {"qualified": False, "status": "unavailable", "error": None,
                                    "connection_label": None, "provider": None, "model": None,
                                    "report_id": None, "configuration_version": None}
            seats.append({"seat": profile.seat, "nickname": profile.nickname,
                          "connection_id": profile.connection_id, **checked[key]})
    return {"roster": plan.roster.model_dump(), "plan_fingerprint": content_hash(plan.model_dump()), "seats": seats,
            "all_qualified": all(s["qualified"] for s in seats),
            "unique_connections": len({p.connection_id for p in plan.ai_profiles}),
            "qualification_configurations": len(checked), "model_calls": 0,
            "game_start_enabled": all(s["qualified"] for s in seats)}


def require_plan_qualified(service: QualificationService, user_id: str, plan: TablePlan) -> dict:
    """Trusted real start boundary; always recheck, never trust a browser's earlier report."""
    report = admission_preflight(service, user_id, plan)
    ready = report["all_qualified"] if service.enabled else report["all_ready"]
    if not ready:
        if not service.enabled:
            raise ServiceError(422, "存在不可用的 AI 配置，请重新检查连接、模型与参数")
        raise ServiceError(403, "存在没有有效资格的 AI 席位，不能上桌")
    return report


def connection_from_config(connection_id: str, config: dict) -> ModelConnection:
    return ModelConnection(id=connection_id, label=config["label"], provider=config["provider"],
                           model=config["model"], base_url=config["base_url"], generation=config["generation"],
                           adapter_version=adapter_version(config["provider"], config["model"], config["generation"]))


def configuration_version(builder: PromptBuilder, connection: ModelConnection, persona: PersonaProfile,
                          rules: dict, revision: str) -> str:
    return content_hash({"configuration": builder.configuration_fingerprint(connection, persona, rules),
                         "credential_revision": revision})


def configuration_preflight(vault, user_id: str, plan: TablePlan, *, opening_policy=DEFAULT_OPENING_POLICY) -> dict:
    """No exam, no requests, no credential decryption; freeze precisely what will run."""
    builder = PromptBuilder(Rulebook.load(), protocol=PLAYER_PROTOCOL, opening_policy=opening_policy)
    rules = asdict(plan.roster.engine_config())
    seats = []
    with vault.lock:
        for profile in sorted(plan.ai_profiles, key=lambda p: p.seat):
            result = {"ready": False, "qualified": False, "status": "unavailable", "error": None,
                      "connection_label": None, "provider": None, "model": None,
                      "report_id": None, "configuration_version": None,
                      "thinking_mode": "disabled", "thinking_output_tokens": None, "thinking_limits": None}
            try:
                config, revision = vault.connection_snapshot(user_id, profile.connection_id)
                c = connection_from_config(profile.connection_id, config)
                result.update(connection_label=c.label, provider=c.provider, model=c.model,
                              thinking_mode=c.generation.thinking_mode,
                              thinking_output_tokens=c.generation.thinking_output_tokens if c.thinking_enabled else None,
                              thinking_limits=V10_BUDGET_POLICY.preview(c))
                validate_connection(c)
                result.update(ready=True, status="ready", configuration_version=configuration_version(
                    builder, c, profile.persona, rules, revision))
            except ProviderError as exc:
                result.update(status="unsupported", error=exc.code)
            except (ServiceError, ValueError):
                pass
            seats.append({"seat": profile.seat, "nickname": profile.nickname,
                          "connection_id": profile.connection_id, **result})
    ready = all(s["ready"] for s in seats)
    return {"roster": plan.roster.model_dump(), "plan_fingerprint": content_hash(plan.model_dump()),
            "seats": seats, "all_ready": ready, "all_qualified": False, "qualification_required": False,
            "unique_connections": len({p.connection_id for p in plan.ai_profiles}),
            "qualification_configurations": 0, "model_calls": 0, "game_start_enabled": ready}
