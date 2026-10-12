"""User-triggered exams, deterministic grading, durable attempts, scoped caching."""

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from time import perf_counter

from starlette.concurrency import run_in_threadpool

from .accounts import AccountStore
from .agent import InvalidModelResponse, ModelAdapter, ModelUsage, parse_action
from .deepseek import ProviderError
from .profiles import ModelConnection, PersonaProfile, SeatProfile, canonical_json, content_hash
from .prompts import PromptBuilder
from .providers import adapter_version, build_adapter, preflight
from .qualification_questions import prepare_question, questions, suite_fingerprint
from .roster import RosterConfig
from .rulebook import Rulebook
from .service import ServiceError

AdapterFactory = Callable[[str], ModelAdapter]


def usage_summary(attempts: list[dict]) -> dict:
    def known(field):
        return sum(a["usage"][field] for a in attempts if a["usage"][field] is not None)
    return {"calls": len(attempts), "known_input_tokens": known("input_tokens"),
            "known_output_tokens": known("output_tokens"),
            "unknown_usage_calls": sum(a["usage"]["input_tokens"] is None or a["usage"]["output_tokens"] is None
                                       for a in attempts),
            "known_cached_input_tokens": known("cached_input_tokens"),
            "known_reasoning_tokens": known("reasoning_tokens"),
            "unknown_cached_usage_calls": sum(a["usage"]["cached_input_tokens"] is None for a in attempts),
            "unknown_reasoning_usage_calls": sum(a["usage"]["reasoning_tokens"] is None for a in attempts),
            "estimated_cost": None}


class QualificationService:
    def __init__(self, vault: AccountStore, factory: AdapterFactory | None = None, *, timeout: float = 35,
                 retry_delay: float = 1, enabled: bool = True):
        self.vault, self.factory = vault, factory
        self.enabled = enabled
        self.timeout, self.retry_delay = timeout, retry_delay
        self.lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.shutdown = False
        self.batch_reservation: str | None = None
        with vault.lock:
            vault.db.executescript("""
                CREATE TABLE IF NOT EXISTS qualifications (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL REFERENCES users(id), connection_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, key_revision TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qualification_operations (
                    user_id TEXT NOT NULL REFERENCES users(id), operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES qualifications(id),
                    PRIMARY KEY(user_id,operation_id)
                );
            """)
            with vault.db:
                for row in vault.db.execute("SELECT id,payload FROM qualifications").fetchall():
                    report = json.loads(row["payload"])
                    if report["status"] == "running":
                        for attempt in report["attempts"]:
                            if attempt["status"] == "in_flight":
                                attempt.update(status="interrupted", error="server_restart")
                        report.update(status="incomplete", error="server_restart", qualified=False)
                        self._write(report)

    def _write(self, report: dict) -> None:
        report["usage"] = usage_summary(report["attempts"])
        self.vault.db.execute("UPDATE qualifications SET payload=? WHERE id=?",
                              (canonical_json(report), report["id"]))

    def persist(self, report: dict) -> None:
        with self.vault.lock, self.vault.db:
            self._write(report)

    def report(self, user_id: str, run_id: str) -> dict:
        with self.vault.lock:
            row = self.vault.db.execute("SELECT payload FROM qualifications WHERE id=? AND user_id=?",
                                       (run_id, user_id)).fetchone()
            if row is None:
                raise ServiceError(404, "资格报告不存在")
            report = json.loads(row[0])
        if not self.enabled:
            return {**report, "qualified": False, "archived": True}
        # Archived passes are not active grants after config/key/Skill changes.
        try:
            _, _, fingerprint, revision, _ = self.configuration(user_id, report["connection_id"],
                                                               PersonaProfile(**report["persona"]),
                                                               RosterConfig(**report.get("roster", {})))
            current = self.latest(user_id, fingerprint, revision)
            report["qualified"] = bool(report["qualified"] and current and current["id"] == run_id)
        except (ServiceError, ValueError):
            report["qualified"] = False
        return report

    def configuration(self, user_id: str, connection_id: str, persona: PersonaProfile, roster: RosterConfig | None = None):
        config, revision = self.vault.connection_snapshot(user_id, connection_id)
        connection = ModelConnection(id=connection_id, provider=config["provider"], model=config["model"],
                                     base_url=config["base_url"], generation=config["generation"],
                                     adapter_version=adapter_version(config["provider"], config["model"], config["generation"]))
        builder = PromptBuilder(Rulebook.load())
        profile = SeatProfile(seat=1, connection_id=connection_id, persona=persona)
        rules = asdict((roster or RosterConfig()).engine_config())
        first = prepare_question(builder, profile, connection, questions(rules)[0])
        fingerprint = content_hash({"configuration": first.configuration_fingerprint,
                                    "suite": suite_fingerprint(rules)})
        version = content_hash({"fingerprint": fingerprint, "credential_revision": revision})
        return connection, builder, fingerprint, revision, version

    def latest(self, user_id: str, fingerprint: str, revision: str) -> dict | None:
        with self.vault.lock:
            row = self.vault.db.execute("SELECT payload FROM qualifications WHERE user_id=? AND fingerprint=? "
                                       "AND key_revision=? ORDER BY seq DESC LIMIT 1",
                                       (user_id, fingerprint, revision)).fetchone()
            return json.loads(row[0]) if row else None

    def preview(self, user_id: str, connection_id: str, persona: PersonaProfile, roster: RosterConfig | None = None) -> dict:
        if not self.enabled:
            raise ServiceError(410, "资格测试已取消，请使用开局配置检查")
        roster = roster or RosterConfig()
        rules = asdict(roster.engine_config())
        connection, builder, fingerprint, revision, version = self.configuration(user_id, connection_id, persona, roster)
        try:
            preflight(prepare_question(builder, SeatProfile(seat=1, connection_id=connection_id, persona=persona),
                                       connection, questions(rules)[0]))
            unsupported = None
        except ProviderError as exc:
            unsupported = exc.code
        latest = self.latest(user_id, fingerprint, revision)
        return {"fingerprint": fingerprint, "configuration_version": version, "unsupported": unsupported,
                "roster": roster.model_dump(),
                "questions": [{"id": q.id, "title": q.title, "category": q.category} for q in questions(rules)],
                "latest_report": latest, "cached": bool(latest and latest["qualified"]),
                "normal_calls": len(questions(rules)), "maximum_calls": len(questions(rules)) * 3,
                "thinking_mode": "requested_disabled" if connection.provider == "siliconflow" else "disabled",
                "simulated_transport": bool(getattr(self.factory, "simulated", False))}

    async def start(self, user_id: str, connection_id: str, persona: PersonaProfile, operation_id: str,
                    expected_version: str, confirm_cost: bool, force: bool, roster: RosterConfig | None = None,
                    *, batch_id: str | None = None) -> dict:
        if not self.enabled:
            raise ServiceError(410, "资格测试已取消，不会发起模型调用")
        roster = roster or RosterConfig()
        rules = asdict(roster.engine_config())
        if not confirm_cost:
            raise ServiceError(422, "必须明确确认本次测试会产生供应商调用和费用")
        async with self.lock:
            request_hash = content_hash({"connection": connection_id, "persona": persona.model_dump(),
                                         "expected_version": expected_version, "force": force,
                                         "roster": roster.model_dump()})
            with self.vault.lock:
                receipt = self.vault.db.execute("SELECT request_hash,run_id FROM qualification_operations "
                                                "WHERE user_id=? AND operation_id=?",
                                                (user_id, operation_id)).fetchone()
            if receipt:
                if receipt["request_hash"] != request_hash:
                    raise ServiceError(409, "操作编号已用于不同的考试请求")
                return await run_in_threadpool(self.report, user_id, receipt["run_id"])
            if self.batch_reservation and self.batch_reservation != batch_id:
                raise ServiceError(409, "一键测试正在运行，请先等待或取消")
            connection, builder, fingerprint, revision, version = await run_in_threadpool(
                self.configuration, user_id, connection_id, persona, roster
            )
            if version != expected_version:
                raise ServiceError(409, "连接或规则已变化，请重新预览后确认")
            profile = SeatProfile(seat=1, connection_id=connection_id, persona=persona)
            try:
                preflight(prepare_question(builder, profile, connection, questions(rules)[0]))
            except ProviderError as exc:
                raise ServiceError(422, f"当前配置不支持资格测试：{exc.code}") from None
            latest = await run_in_threadpool(self.latest, user_id, fingerprint, revision)
            if latest and latest["qualified"] and not force:
                with self.vault.lock, self.vault.db:
                    self.vault.db.execute("INSERT INTO qualification_operations VALUES(?,?,?,?)",
                                          (user_id, operation_id, request_hash, latest["id"]))
                return latest
            if any(not task.done() for task in self.tasks.values()):
                raise ServiceError(409, "已有资格测试正在运行，请等它结束或取消后再试")
            run_id = uuid.uuid4().hex
            report = {"id": run_id, "connection_id": connection_id, "fingerprint": fingerprint,
                      "provider": connection.provider, "model": connection.model, "persona": persona.model_dump(),
                      "roster": roster.model_dump(),
                      "rule_total": sum(q.category == "rule" for q in questions(rules)), "logic_total": 6,
                      "question_total": len(questions(rules)),
                      "status": "running", "qualified": False, "simulated": False, "error": None,
                      "rule_passed": 0, "logic_passed": 0, "results": [], "attempts": [], "usage": usage_summary([])}
            with self.vault.lock, self.vault.db:
                self.vault.db.execute("INSERT INTO qualifications(id,user_id,connection_id,fingerprint,key_revision,payload) "
                                      "VALUES(?,?,?,?,?,?)", (run_id, user_id, connection_id, fingerprint,
                                                             revision, canonical_json(report)))
                self.vault.db.execute("INSERT INTO qualification_operations VALUES(?,?,?,?)",
                                      (user_id, operation_id, request_hash, run_id))
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
            self.tasks[run_id] = asyncio.create_task(self._run(user_id, connection, profile, builder, revision, report))
            return report

    async def _run(self, user_id: str, connection: ModelConnection, profile: SeatProfile,
                   builder: PromptBuilder, revision: str, report: dict):
        try:
            # This is the only path that decrypts credentials, after explicit start/ownership/CSRF checks.
            credential = await run_in_threadpool(self.vault.credential, user_id, connection.id)
            adapter = self.factory(credential) if self.factory else build_adapter(connection, credential)
            report["simulated"] = bool(getattr(adapter, "simulated", False))
            roster = RosterConfig(**report.get("roster", {}))
            for question in questions(asdict(roster.engine_config())):
                request = prepare_question(builder, profile, connection, question)
                for retry in range(3):
                    _, _, current_fp, current_revision, _ = await run_in_threadpool(
                        self.configuration, user_id, connection.id, profile.persona, roster
                    )
                    if current_revision != revision or current_fp != report["fingerprint"]:
                        raise ProviderError("configuration_changed")
                    attempt = {"id": uuid.uuid4().hex, "provider": connection.provider, "model": connection.model,
                               "seat": profile.seat, "purpose": "qualification", "question_id": question.id,
                               "retry": retry, "status": "in_flight", "latency_ms": None, "error": None,
                               "configuration_fingerprint": report["fingerprint"], "call_count": 1,
                               "input_characters": request.input_characters, "output_limit": request.max_output_tokens,
                               "warnings": list(request.warnings),
                               "usage": asdict(ModelUsage()), "simulated": report["simulated"]}
                    report["attempts"].append(attempt)
                    await run_in_threadpool(self.persist, report)
                    start = perf_counter()
                    response = None
                    try:
                        async with asyncio.timeout(self.timeout):
                            response = await adapter.complete(request)
                        attempt["usage"] = asdict(response.usage)
                        attempt["simulated"] = report["simulated"] or response.simulated
                        report["simulated"] = report["simulated"] or response.simulated
                        action = parse_action(response.content, question.view["legal_actions"][0])
                    except InvalidModelResponse:
                        # Invalid answers (including bad JSON/action format) are failed questions,
                        # not corrected-and-retried exam answers.
                        attempt.update(status="invalid_response", error="invalid_action",
                                       latency_ms=(perf_counter() - start) * 1000)
                        report["results"].append({"id": question.id, "title": question.title,
                                                  "category": question.category, "passed": False,
                                                  "answer": None, "error": "invalid_action"})
                        break
                    except (ProviderError, TimeoutError) as exc:
                        attempt["error"] = exc.code if isinstance(exc, ProviderError) else (
                            "timeout"
                        )
                        attempt["status"] = "error"
                        if isinstance(exc, ProviderError):
                            attempt["usage"] = asdict(exc.usage)
                        attempt["latency_ms"] = (perf_counter() - start) * 1000
                        await run_in_threadpool(self.persist, report)
                        retryable = exc.retryable if isinstance(exc, ProviderError) else True
                        if retryable and retry < 2:
                            await asyncio.sleep(self.retry_delay * (retry + 1))
                            continue
                        raise ProviderError(attempt["error"]) from None
                    except asyncio.CancelledError:
                        attempt.update(status="interrupted" if self.shutdown else "cancelled", error="usage_unknown",
                                       latency_ms=(perf_counter() - start) * 1000)
                        # Await the ledger write even if the caller has disconnected.
                        await run_in_threadpool(self.persist, report)
                        raise
                    else:
                        attempt.update(status="ok", latency_ms=(perf_counter() - start) * 1000)
                        passed = question.category == "rule" or action.target == question.expected_target
                        report["results"].append({"id": question.id, "title": question.title,
                                                  "category": question.category, "passed": passed,
                                                  "answer": {"kind": action.kind, "target": action.target,
                                                             "potion": action.potion,
                                                             "text_characters": len(action.text or "")},
                                                  "error": None if passed else "wrong_answer"})
                        if passed:
                            report[f"{question.category}_passed"] += 1
                        break
                await run_in_threadpool(self.persist, report)
            passed = report["rule_passed"] == report["rule_total"] and report["logic_passed"] >= 5
            report.update(status="passed" if passed else "failed", qualified=passed and not report["simulated"])
        except asyncio.CancelledError:
            report.update(status="incomplete" if self.shutdown else "cancelled", qualified=False,
                          error="server_shutdown" if self.shutdown else "cancelled")
        except (ProviderError, ServiceError) as exc:
            report.update(status="incomplete", qualified=False,
                          error=exc.code if isinstance(exc, ProviderError) else "connection_unavailable")
        except Exception:  # noqa: BLE001 - sanitize arbitrary provider-plugin failures, never echo/log secrets.
            # Never log raw provider exceptions: they can embed Authorization or response bodies.
            report.update(status="incomplete", qualified=False, error="internal_error")
        finally:
            for attempt in report["attempts"]:
                if attempt["status"] == "in_flight":
                    attempt.update(status="error", error=report["error"] or "internal_error")
            await run_in_threadpool(self.persist, report)

    async def cancel(self, user_id: str, run_id: str) -> dict:
        await run_in_threadpool(self.report, user_id, run_id)
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Cancellation before the coroutine starts cannot execute its finally block.
            report = await run_in_threadpool(self.report, user_id, run_id)
            if report["status"] == "running":
                report.update(status="cancelled", qualified=False, error="cancelled")
                await run_in_threadpool(self.persist, report)
        return await run_in_threadpool(self.report, user_id, run_id)

    async def cancel_user(self, user_id: str):
        with self.vault.lock:
            ids = [row[0] for row in self.vault.db.execute("SELECT id FROM qualifications WHERE user_id=?",
                                                        (user_id,)) if row[0] in self.tasks]
        for run_id in ids:
            await self.cancel(user_id, run_id)

    def require_qualified(self, user_id: str, connection_id: str, persona: PersonaProfile) -> dict:
        """Trusted admission boundary. Real game creation remains disabled in this delivery."""
        preview = self.preview(user_id, connection_id, persona)
        if preview["unsupported"] or not preview["cached"]:
            raise ServiceError(403, "当前模型与个性未通过有效资格测试，不能上桌")
        return preview["latest_report"]

    async def close(self):
        self.shutdown = True
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with self.vault.lock, self.vault.db:
            for row in self.vault.db.execute("SELECT payload FROM qualifications").fetchall():
                report = json.loads(row[0])
                if report["status"] == "running":
                    report.update(status="incomplete", qualified=False, error="server_shutdown")
                    for attempt in report["attempts"]:
                        if attempt["status"] == "in_flight":
                            attempt.update(status="interrupted", error="usage_unknown")
                    self._write(report)
