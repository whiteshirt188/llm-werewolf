"""Explicitly consented, deduplicated, durable qualification queues. Never auto-resume."""

import asyncio
import json
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from .profiles import PersonaProfile, canonical_json, content_hash
from .qualification import QualificationService, usage_summary
from .service import ServiceError
from .table_plan import TablePlan


class QualificationBatches:
    def __init__(self, service: QualificationService):
        self.service, self.vault = service, service.vault
        self.tasks: dict[str, asyncio.Task] = {}
        self.shutdown = False
        with self.vault.lock, self.vault.db:
            self.vault.db.execute("CREATE TABLE IF NOT EXISTS qualification_batches ("
                                  "seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,"
                                  "user_id TEXT NOT NULL,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,"
                                  "payload TEXT NOT NULL,UNIQUE(user_id,operation_id))")
            for row in self.vault.db.execute("SELECT payload FROM qualification_batches").fetchall():
                report = json.loads(row[0])
                if report["status"] == "running":
                    report.update(status="incomplete", error="server_restart")
                    self.persist(report)

    def preview(self, user_id: str, plan: TablePlan) -> dict:
        groups = {}
        # One consistent, read-only configuration snapshot; no key decryption.
        with self.vault.lock:
            for seat in sorted(plan.ai_profiles, key=lambda p: p.seat):
                key = (seat.connection_id, content_hash(seat.persona.decision_config()))
                if key in groups:
                    groups[key]["seats"].append(seat.seat)
                    continue
                preview = self.service.preview(user_id, seat.connection_id, seat.persona, plan.roster)
                groups[key] = {"connection_id": seat.connection_id, "persona": seat.persona.model_dump(),
                               "seats": [seat.seat], "configuration_version": preview["configuration_version"],
                               "cached": preview["cached"], "unsupported": preview["unsupported"],
                               "normal_calls": preview["normal_calls"], "maximum_calls": preview["maximum_calls"],
                               "thinking_mode": preview["thinking_mode"],
                               "report_id": preview["latest_report"]["id"] if preview["cached"] else None,
                               "status": "cached" if preview["cached"] else "pending"}
        items = list(groups.values())
        result = {"roster": plan.roster.model_dump(), "groups": items,
                  "normal_calls": sum(g["normal_calls"] for g in items if not g["cached"]),
                  "maximum_calls": sum(g["maximum_calls"] for g in items if not g["cached"]),
                  "simulated_transport": bool(getattr(self.service.factory, "simulated", False)),
                  "blocked": any(g["unsupported"] for g in items), "model_calls": 0}
        result["version"] = content_hash(result)
        return result

    def persist(self, report: dict):
        with self.vault.lock, self.vault.db:
            self.vault.db.execute("UPDATE qualification_batches SET payload=? WHERE id=?",
                                  (canonical_json(report), report["id"]))

    def report(self, user_id: str, batch_id: str) -> dict:
        with self.vault.lock:
            row = self.vault.db.execute("SELECT payload FROM qualification_batches WHERE id=? AND user_id=?",
                                       (batch_id, user_id)).fetchone()
            if row is None:
                raise ServiceError(404, "一键测试记录不存在")
            report = json.loads(row[0])
        attempts = []
        for group in report["groups"]:
            if group["report_id"]:
                exam = self.service.report(user_id, group["report_id"])
                if not group["cached"]:
                    attempts.extend(exam["attempts"])
                group["exam"] = {k: exam[k] for k in ("status", "qualified", "simulated", "error", "rule_passed", "logic_passed")}
                group["exam"].update(rule_total=exam.get("rule_total", 6), question_total=exam.get("question_total", 12))
                group["exam"]["completed"] = len(exam["results"])
        report["usage"] = usage_summary(attempts)
        return report

    def current(self, user_id: str) -> dict | None:
        with self.vault.lock:
            row = self.vault.db.execute("SELECT id FROM qualification_batches WHERE user_id=? ORDER BY seq DESC LIMIT 1",
                                       (user_id,)).fetchone()
        return self.report(user_id, row[0]) if row else None

    async def start(self, user_id: str, token: str, plan: TablePlan, operation_id: str,
                    expected_version: str, confirm_cost: bool) -> dict:
        if not self.service.enabled:
            raise ServiceError(410, "资格测试已取消，不会发起模型调用")
        if not confirm_cost:
            raise ServiceError(422, "必须明确确认一键测试的供应商调用与费用")
        request_hash = content_hash({"plan": plan.model_dump(), "version": expected_version})
        async with self.service.lock:
            with self.vault.lock:
                receipt = self.vault.db.execute("SELECT id,request_hash FROM qualification_batches WHERE user_id=? AND operation_id=?",
                                               (user_id, operation_id)).fetchone()
            if receipt:
                if receipt["request_hash"] != request_hash:
                    raise ServiceError(409, "操作编号已用于其他测试配置")
                return await run_in_threadpool(self.report, user_id, receipt["id"])
            preview = await run_in_threadpool(self.preview, user_id, plan)
            if preview["version"] != expected_version:
                raise ServiceError(409, "连接、资格或规则已变化，请重新预览并确认")
            if preview["blocked"]:
                raise ServiceError(422, "存在不支持的连接，请先修改席位配置")
            if self.service.batch_reservation or any(not t.done() for t in self.service.tasks.values()):
                raise ServiceError(409, "已有资格测试正在运行，请等待或取消")
            report = {**preview, "id": uuid4().hex, "status": "running", "error": None}
            with self.vault.lock, self.vault.db:
                self.vault.db.execute("INSERT INTO qualification_batches(id,user_id,operation_id,request_hash,payload) VALUES(?,?,?,?,?)",
                                      (report["id"], user_id, operation_id, request_hash, canonical_json(report)))
            self.service.batch_reservation = report["id"]
            self.tasks[report["id"]] = asyncio.create_task(self._run(user_id, token, plan, report))
        return report

    async def _run(self, user_id: str, token: str, plan: TablePlan, report: dict):
        active = None
        try:
            for index, group in enumerate(report["groups"]):
                if group["cached"]:
                    continue
                session = await run_in_threadpool(self.vault.session, token)
                if not session or session["user"]["id"] != user_id:
                    raise ServiceError(401, "登录失效")
                preview = await run_in_threadpool(self.service.preview, user_id, group["connection_id"],
                                                 PersonaProfile(**group["persona"]), plan.roster)
                if preview["configuration_version"] != group["configuration_version"]:
                    report["error"] = "configuration_changed"
                    break
                exam = await self.service.start(user_id, group["connection_id"], PersonaProfile(**group["persona"]),
                                                f"batch:{report['id']}:{index}", group["configuration_version"], True, False,
                                                plan.roster, batch_id=report["id"])
                active = exam["id"]
                group.update(status="running", report_id=active)
                await run_in_threadpool(self.persist, report)
                task = self.service.tasks.get(active)
                if task:
                    await asyncio.shield(task)
                exam = await run_in_threadpool(self.service.report, user_id, active)
                group["status"] = exam["status"]
                active = None
                await run_in_threadpool(self.persist, report)
                if exam["status"] in {"incomplete", "cancelled"}:
                    report["error"] = exam["error"]
                    break
            report["status"] = "incomplete" if report["error"] else "completed"
        except asyncio.CancelledError:
            if active:
                await self.service.cancel(user_id, active)
            report.update(status="incomplete" if self.shutdown else "cancelled",
                          error="server_shutdown" if self.shutdown else "cancelled")
        except ServiceError as exc:
            report.update(status="incomplete", error="account_session_expired" if exc.status == 401 else "connection_unavailable")
        except Exception:  # noqa: BLE001 - never persist raw provider or credential-bearing errors
            report.update(status="incomplete", error="internal_error")
        finally:
            for group in report["groups"]:
                if group["status"] == "running":
                    group["status"] = "incomplete" if self.shutdown else "cancelled"
            await run_in_threadpool(self.persist, report)
            if self.service.batch_reservation == report["id"]:
                self.service.batch_reservation = None

    async def cancel(self, user_id: str, batch_id: str):
        report = await run_in_threadpool(self.report, user_id, batch_id)
        task = self.tasks.get(batch_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Handle cancellation before the coroutine first enters its try/finally.
        report = await run_in_threadpool(self.report, user_id, batch_id)
        if report["status"] == "running":
            report.update(status="incomplete" if self.shutdown else "cancelled",
                          error="server_shutdown" if self.shutdown else "cancelled")
            await run_in_threadpool(self.persist, report)
        if self.service.batch_reservation == batch_id:
            self.service.batch_reservation = None
        return await run_in_threadpool(self.report, user_id, batch_id)

    async def cancel_user(self, user_id: str):
        report = await run_in_threadpool(self.current, user_id)
        if report and report["status"] == "running":
            await self.cancel(user_id, report["id"])

    async def close(self):
        self.shutdown = True
        with self.vault.lock:
            running = [(r["user_id"], r["id"]) for r in self.vault.db.execute("SELECT user_id,id,payload FROM qualification_batches")
                       if json.loads(r["payload"])["status"] == "running"]
        for user_id, batch_id in running:
            await self.cancel(user_id, batch_id)
