"""Cookie-authenticated settings and explicit qualification starts; no game authorization."""

import hmac
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.concurrency import run_in_threadpool

from .accounts import SESSION_SECONDS
from .profiles import GenerationParams, PersonaProfile
from .roster import RosterConfig
from .service import ServiceError
from .table_plan import TablePlan, admission_preflight

router = APIRouter(prefix="/api/account", tags=["本机账号与模型设置"])
COOKIE = "werewolf_account"


class AccountInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Credentials(AccountInput):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    password: SecretStr = Field(min_length=10, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower()


class PasswordInput(AccountInput):
    old_password: SecretStr = Field(min_length=10, max_length=128)
    new_password: SecretStr = Field(min_length=10, max_length=128)


class ConnectionInput(AccountInput):
    label: str = Field(min_length=1, max_length=64)
    provider: Literal["deepseek", "dashscope", "siliconflow"]
    model: str = Field(min_length=1, max_length=128)
    generation: GenerationParams = Field(default_factory=GenerationParams)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("label", "model")
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError("不能为空")
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None and (not value.get_secret_value().strip() or any(
            c.isspace() for c in value.get_secret_value()
        )):
            raise ValueError("密钥不能包含空白")
        return value


class DeleteInput(AccountInput):
    confirm: Literal[True]


class QualificationPreview(AccountInput):
    roster: RosterConfig = Field(default_factory=RosterConfig)
    connection_id: str = Field(min_length=1, max_length=128)
    persona: PersonaProfile = Field(default_factory=PersonaProfile)


class QualificationStart(QualificationPreview):
    operation_id: str = Field(min_length=1, max_length=128)
    expected_version: str = Field(min_length=1, max_length=128)
    confirm_cost: bool = False
    force_retest: bool = False


class SaveTablePlan(AccountInput):
    plan: TablePlan
    expected_version: str | None = Field(default=None, min_length=1, max_length=128)


class RealGameStart(AccountInput):
    plan: TablePlan
    operation_id: str = Field(min_length=1, max_length=128)
    expected_versions: dict[str, str]
    confirm_cost: bool = False


class BatchStart(AccountInput):
    plan: TablePlan
    operation_id: str = Field(min_length=1, max_length=128)
    expected_version: str = Field(min_length=1, max_length=128)
    confirm_cost: bool = False


class RealGameAuthorization(AccountInput):
    confirm_cost: Literal[True]


def guard(request: Request) -> None:
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ServiceError(403, "账号设置仅支持本机 localhost 或 127.0.0.1 访问")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ServiceError(403, "不允许跨站访问账号设置")
    origin = request.headers.get("origin")
    if origin:
        expected = urlsplit(str(request.base_url))
        actual = urlsplit(origin)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise ServiceError(403, "请求来源不匹配，请从本机网页操作")
    if request.method != "GET" and request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ServiceError(415, "请使用 JSON 请求")


async def authorized(request: Request, *, mutation: bool = False) -> dict:
    guard(request)
    session = await run_in_threadpool(request.app.state.accounts.session, request.cookies.get(COOKIE))
    if session is None:
        raise ServiceError(401, "请先登录本机账号")
    if mutation and not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(),
                                            session["csrf_token"].encode()):
        raise ServiceError(403, "登录状态已变化，请重新打开模型设置")
    return session


def cookie(response: Response, request: Request, token: str):
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/api/account")


@router.get("/status")
async def status(request: Request, response: Response):
    guard(request)
    current = await run_in_threadpool(request.app.state.accounts.session, request.cookies.get(COOKIE))
    if current is None:
        response.delete_cookie(COOKIE, path="/api/account")
    return current or {"user": None, "csrf_token": None}


async def authenticate(body: Credentials, request: Request, response: Response, kind: str):
    guard(request)
    # Only one scrypt worker per local vault; game scheduling is not blocked.
    result, token = await run_in_threadpool(getattr(request.app.state.accounts, kind),
                                           body.username, body.password.get_secret_value(),
                                           request.client.host if request.client else "local")
    old = request.cookies.get(COOKIE)
    if old:
        previous = await run_in_threadpool(request.app.state.accounts.session, old)
        if previous:
            await request.app.state.service.pause_owner(previous["user"]["id"])
        await run_in_threadpool(request.app.state.accounts.logout, old)
    cookie(response, request, token)
    return result


@router.post("/register", status_code=201)
async def register(body: Credentials, request: Request, response: Response):
    return await authenticate(body, request, response, "register")


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response):
    return await authenticate(body, request, response, "login")


@router.post("/logout")
async def logout(request: Request, response: Response):
    current = await authorized(request, mutation=True)
    await request.app.state.qualification_batches.cancel_user(current["user"]["id"])
    await request.app.state.qualifications.cancel_user(current["user"]["id"])
    await request.app.state.service.pause_owner(current["user"]["id"])
    await run_in_threadpool(request.app.state.accounts.logout, request.cookies[COOKIE])
    response.delete_cookie(COOKIE, path="/api/account")
    return {"user": None, "csrf_token": None}


@router.post("/password")
async def change_password(body: PasswordInput, request: Request, response: Response):
    current = await authorized(request, mutation=True)
    result, token = await run_in_threadpool(request.app.state.accounts.change_password,
                                           current["user"]["id"], body.old_password.get_secret_value(),
                                           body.new_password.get_secret_value(),
                                           request.client.host if request.client else "local")
    await request.app.state.qualification_batches.cancel_user(current["user"]["id"])
    await request.app.state.qualifications.cancel_user(current["user"]["id"])
    await request.app.state.service.pause_owner(current["user"]["id"])
    cookie(response, request, token)
    return result


@router.get("/connections")
async def connections(request: Request):
    current = await authorized(request)
    return {"connections": await run_in_threadpool(request.app.state.accounts.connections, current["user"]["id"])}


async def save(body: ConnectionInput, request: Request, connection_id: str | None):
    current = await authorized(request, mutation=True)
    return await run_in_threadpool(request.app.state.accounts.save_connection,
        current["user"]["id"], connection_id, body.model_dump(exclude={"api_key"}),
        body.api_key.get_secret_value() if body.api_key is not None else None,
    )


@router.post("/connections", status_code=201)
async def create_connection(body: ConnectionInput, request: Request):
    return await save(body, request, None)


@router.post("/connections/{connection_id}")
async def update_connection(connection_id: str, body: ConnectionInput, request: Request):
    return await save(body, request, connection_id)


@router.post("/connections/{connection_id}/delete")
async def delete_connection(connection_id: str, body: DeleteInput, request: Request):
    current = await authorized(request, mutation=True)
    await run_in_threadpool(request.app.state.accounts.delete_connection, current["user"]["id"], connection_id)
    return {"deleted": True}


@router.post("/qualifications/preview")
async def qualification_preview(body: QualificationPreview, request: Request):
    current = await authorized(request, mutation=True)
    return await run_in_threadpool(request.app.state.qualifications.preview, current["user"]["id"],
                                       body.connection_id, body.persona, body.roster)


@router.post("/qualification-batches/preview")
async def batch_preview(body: TablePlan, request: Request):
    current = await authorized(request, mutation=True)
    if not request.app.state.qualifications.enabled:
        raise ServiceError(410, "资格测试已取消，请使用开局配置检查")
    return await run_in_threadpool(request.app.state.qualification_batches.preview, current["user"]["id"], body)


@router.post("/qualification-batches", status_code=202)
async def batch_start(body: BatchStart, request: Request):
    current = await authorized(request, mutation=True)
    return await request.app.state.qualification_batches.start(current["user"]["id"], request.cookies[COOKIE],
                                                              body.plan, body.operation_id, body.expected_version,
                                                              body.confirm_cost)


@router.get("/qualification-batches/current")
async def batch_current(request: Request):
    current = await authorized(request)
    return {"batch": await run_in_threadpool(request.app.state.qualification_batches.current, current["user"]["id"])}


@router.get("/qualification-batches/{batch_id}")
async def batch_report(batch_id: str, request: Request):
    current = await authorized(request)
    return await run_in_threadpool(request.app.state.qualification_batches.report, current["user"]["id"], batch_id)


@router.post("/qualification-batches/{batch_id}/cancel")
async def batch_cancel(batch_id: str, request: Request):
    current = await authorized(request, mutation=True)
    return await request.app.state.qualification_batches.cancel(current["user"]["id"], batch_id)


@router.post("/qualifications", status_code=202)
async def start_qualification(body: QualificationStart, request: Request):
    current = await authorized(request, mutation=True)
    return await request.app.state.qualifications.start(current["user"]["id"], body.connection_id, body.persona,
                                                        body.operation_id, body.expected_version,
                                                        body.confirm_cost, body.force_retest, body.roster)


@router.get("/qualifications/{run_id}")
async def qualification_report(run_id: str, request: Request):
    current = await authorized(request)
    return await run_in_threadpool(request.app.state.qualifications.report, current["user"]["id"], run_id)


@router.post("/qualifications/{run_id}/cancel")
async def cancel_qualification(run_id: str, request: Request):
    current = await authorized(request, mutation=True)
    return await request.app.state.qualifications.cancel(current["user"]["id"], run_id)


@router.get("/table-plan")
async def table_plan(request: Request):
    current = await authorized(request)
    return await run_in_threadpool(request.app.state.accounts.table_plan, current["user"]["id"])


@router.post("/table-plan")
async def save_table_plan(body: SaveTablePlan, request: Request):
    current = await authorized(request, mutation=True)
    return await run_in_threadpool(request.app.state.accounts.save_table_plan, current["user"]["id"],
                                   body.plan.model_dump(), body.expected_version)


@router.post("/table-plan/admission")
async def preview_table_admission(body: TablePlan, request: Request):
    current = await authorized(request, mutation=True)
    return await run_in_threadpool(admission_preflight, request.app.state.qualifications,
                                   current["user"]["id"], body)


def game_cookie(response: Response, game_id: str, token: str):
    response.set_cookie("werewolf_session", token, httponly=True, samesite="strict", path=f"/api/games/{game_id}")


@router.post("/games", status_code=201)
async def start_real_game(body: RealGameStart, request: Request, response: Response):
    current = await authorized(request, mutation=True)
    state, token = await request.app.state.service.create_real(
        current["user"]["id"], request.cookies[COOKIE], body.plan, body.operation_id, body.expected_versions, body.confirm_cost
    )
    game_cookie(response, state["game_id"], token)
    return {"game": state, "session_token": token}


@router.get("/games/current")
async def current_real_game(request: Request, response: Response):
    current = await authorized(request)
    service = request.app.state.service
    runtime = next((g for g in service.games.values() if g.real_binding and
                    g.real_binding["owner_id"] == current["user"]["id"] and g.status in {"running", "paused"}), None)
    if runtime is None:
        receipt = service.store.latest_start(current["user"]["id"])
        recent = None
        if receipt:
            token = service.real_gateway.decrypt_token(receipt["encrypted_token"])
            game_cookie(response, receipt["game_id"], token)
            recent = {"game": service.games[receipt["game_id"]].visible(), "session_token": token}
        return {"game": None, "session_token": None, "last_game": recent}
    async with runtime.lock:
        receipt = service.store.start_for_game(current["user"]["id"], runtime.id)
        token = service.real_gateway.decrypt_token(receipt["encrypted_token"])
        game_cookie(response, runtime.id, token)
        return {"game": runtime.visible(), "session_token": token, "last_game": None}


@router.post("/games/{game_id}/authorize")
async def authorize_real_game(game_id: str, body: RealGameAuthorization, request: Request):
    current = await authorized(request, mutation=True)
    service = request.app.state.service
    runtime = service.games.get(game_id)
    if not runtime or not runtime.real_binding or runtime.real_binding["owner_id"] != current["user"]["id"]:
        raise ServiceError(404, "对局不存在")
    if runtime.status != "paused":
        raise ServiceError(409, "只能重新授权已暂停的对局")
    await service.rebind_real(runtime, current["user"]["id"], request.cookies[COOKIE])
    return runtime.visible()
