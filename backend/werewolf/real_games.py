"""Trusted account/credential boundary for explicitly confirmed real game starts."""

import time
from dataclasses import asdict

from .accounts import token_hash
from .deepseek import ProviderError
from .opening import DEFAULT_OPENING_POLICY
from .player_state import LEGACY_PROTOCOL, PLAYER_PROTOCOL
from .profiles import ModelConnection, SeatProfile, content_hash
from .prompts import PromptBuilder
from .providers import adapter_version, build_adapter
from .qualification_questions import prepare_question, questions, suite_fingerprint
from .rulebook import Rulebook
from .service import ServiceError
from .table_plan import configuration_version, require_plan_qualified


class RealGameGateway:
    def __init__(self, vault, qualifications, factory=None, *, opening_policy=DEFAULT_OPENING_POLICY):
        self.vault, self.qualifications = vault, qualifications
        self.factory = factory
        self.opening_policy = opening_policy

    def prepare(self, user_id, session_token, plan, expected_versions):
        with self.vault.lock:
            report = require_plan_qualified(self.qualifications, user_id, plan)
            current = {str(s["seat"]): s["configuration_version"] for s in report["seats"]}
            if current != expected_versions:
                raise ServiceError(409, "模型配置或规则已变化，请重新预检并确认费用")
            bindings, connections = {}, []
            for connection_id in sorted({p.connection_id for p in plan.ai_profiles}):
                config, revision = self.vault.connection_snapshot(user_id, connection_id)
                c = ModelConnection(id=connection_id, label=config["label"], provider=config["provider"],
                                    model=config["model"], base_url=config["base_url"], generation=config["generation"],
                                    adapter_version=adapter_version(config["provider"], config["model"], config["generation"]))
                connections.append(c)
                bindings[connection_id] = {"revision": revision, "decision_hash": content_hash(c.decision_config())}
            binding = {"owner_id": user_id, "session_hash": token_hash(session_token), "connections": bindings,
                       "qualification_required": self.qualifications.enabled,
                       "qualification_reports": {str(s["seat"]): s["report_id"] for s in report["seats"]
                                                 if s["report_id"] is not None}}
            profiles = [SeatProfile(**p.model_dump()) for p in plan.ai_profiles]
            book = Rulebook.load()
            builder = PromptBuilder(book, protocol=LEGACY_PROTOCOL if self.qualifications.enabled else PLAYER_PROTOCOL,
                                    opening_policy=self.opening_policy)
            # Freeze exactly the rules admitted above, including a possible concurrent file edit.
            for profile in profiles:
                c = next(c for c in connections if c.id == profile.connection_id)
                rules = asdict(plan.roster.engine_config())
                if self.qualifications.enabled:  # Historical exam regressions only; production defaults off.
                    exam_profile = SeatProfile(seat=1, connection_id=c.id, persona=profile.persona)
                    first = prepare_question(builder, exam_profile, c, questions(rules)[0])
                    fingerprint = content_hash({"configuration": first.configuration_fingerprint, "suite": suite_fingerprint(rules)})
                    version = content_hash({"fingerprint": fingerprint, "credential_revision": bindings[c.id]["revision"]})
                else:
                    version = configuration_version(builder, c, profile.persona, rules, bindings[c.id]["revision"])
                if current[str(profile.seat)] != version:
                    raise ServiceError(409, "规则资料在开局检查中变化，请重新预检")
            self.check_session(binding)
            return connections, profiles, book, binding

    def check_session(self, binding):
        with self.vault.lock:
            row = self.vault.db.execute("SELECT user_id,expires FROM sessions WHERE token_hash=?",
                                        (binding["session_hash"],)).fetchone()
            if row is None or row["user_id"] != binding["owner_id"] or row["expires"] <= time.time():
                raise ProviderError("account_session_expired")

    def adapter(self, runtime, connection):
        # Validate before every actual attempt. Never decrypt during GET/replay.
        with self.vault.lock:
            self.check_session(runtime.real_binding)
            try:
                config, revision = self.vault.connection_snapshot(runtime.real_binding["owner_id"], connection.id)
            except ServiceError:
                raise ProviderError("connection_unavailable") from None
            frozen = runtime.real_binding["connections"][connection.id]
            current = ModelConnection(id=connection.id, provider=config["provider"], model=config["model"],
                                      base_url=config["base_url"], generation=config["generation"],
                                      adapter_version=adapter_version(config["provider"], config["model"], config["generation"]))
            if revision != frozen["revision"] or content_hash(current.decision_config()) != frozen["decision_hash"]:
                raise ProviderError("configuration_changed")
            credential = self.vault.credential(runtime.real_binding["owner_id"], connection.id)
            return self.factory(credential) if self.factory else build_adapter(connection, credential)

    def encrypt_token(self, token):
        return self.vault.cipher.encrypt(token.encode()).decode()

    def decrypt_token(self, token):
        return self.vault.cipher.decrypt(token.encode()).decode()

    def rebind_session(self, runtime, user_id, session_token):
        if runtime.real_binding["owner_id"] != user_id:
            raise ServiceError(404, "对局不存在")
        binding = {**runtime.real_binding, "session_hash": token_hash(session_token)}
        self.check_session(binding)
        return binding
