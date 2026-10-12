"""Local-only API. Importing this module does not open a database or start agents."""

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .account_api import router as account_router
from .accounts import AccountStore
from .agent import ModelAdapter
from .decision_budget import V10_BUDGET_POLICY
from .engine import Action
from .profiles import DEFAULT_PLAY_STYLES, ModelConnection, PersonaProfile, SeatProfile, canonical_json
from .prompts import DEFAULT_PHASE_LIMITS, PLAYER_PROMPT_VERSION
from .qualification import AdapterFactory, QualificationService, usage_summary
from .qualification_batches import QualificationBatches
from .real_games import RealGameGateway
from .roster import RosterConfig
from .service import GameService, ServiceError
from .storage import Store


class InputModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def mock_connection() -> list[ModelConnection]:
    return [ModelConnection(id="shared-mock", provider="mock", model="offline-policy", base_url="mock://local")]


class CreateGame(InputModel):
    roster: RosterConfig = Field(default_factory=RosterConfig)
    mode: Literal["mock"] = "mock"
    seed: int | None = None
    human_seat: int = Field(default=1, ge=1, le=9)
    connections: list[ModelConnection] = Field(default_factory=mock_connection, min_length=1, max_length=8)
    ai_profiles: list[SeatProfile] | None = None
    playback_mode: Literal["manual", "auto"] | None = None

    @model_validator(mode="after")
    def validate_seats(self):
        ids = [c.id for c in self.connections]
        if len(set(ids)) != len(ids) or any(c.provider != "mock" for c in self.connections):
            raise ValueError("连接 ID 必须唯一，本阶段只支持 mock 连接")
        if self.ai_profiles is None:
            seats = [s for s in range(1, self.roster.players + 1) if s != self.human_seat]
            self.ai_profiles = [SeatProfile(seat=s, connection_id=ids[0],
                                           persona=PersonaProfile(play_style=DEFAULT_PLAY_STYLES[i]))
                                for i, s in enumerate(seats)]
        seats = [p.seat for p in self.ai_profiles]
        if (self.human_seat > self.roster.players or len(seats) != self.roster.players - 1
                or set(seats) != set(range(1, self.roster.players + 1)) - {self.human_seat}):
            raise ValueError("AI 席位必须覆盖所选人数的所有非人类席位，且不能重复")
        if any(p.connection_id not in ids for p in self.ai_profiles):
            raise ValueError("席位引用了不存在的连接")
        return self


class ActionInput(InputModel):
    kind: str
    target: int | None = None
    text: str | None = Field(default=None, max_length=300)
    potion: Literal["skip", "save", "poison"] | None = None


class SubmitAction(InputModel):
    operation_id: str = Field(min_length=1, max_length=128)
    expected_version: str = Field(min_length=1, max_length=128)
    action: ActionInput


class ControlInput(InputModel):
    operation_id: str = Field(min_length=1, max_length=128)


class ResumeInput(ControlInput):
    recovery: Literal["more_tokens", "without_thinking"] | None = None


class AdvanceInput(ControlInput):
    expected_step: str = Field(min_length=1, max_length=128)


class PlaybackInput(ControlInput):
    mode: Literal["manual", "auto"]


class SessionInput(InputModel):
    token: str = Field(min_length=1, max_length=256)


class GameView(BaseModel):
    game_id: str
    mode: Literal["mock", "real"]
    status: Literal["running", "paused", "finished", "abandoned"]
    simulated_transport: bool = False
    human_seat: int
    pause_reason: str | None
    turn_version: str | None
    reading: bool = False
    playback_mode: Literal["manual", "auto"] = "auto"
    step_waiting: bool = False
    step_id: str | None = None
    speech_to_vote: bool = False
    preparation: dict | None = None
    decision_recovery: dict | None = None
    view: dict
    seat_profiles: list[dict] = Field(default_factory=list)


class CreatedGame(BaseModel):
    game: GameView
    session_token: str


class StreamItem(BaseModel):
    id: int
    state: GameView


class EventBatch(BaseModel):
    events: list[StreamItem]
    cursor: int


def create_app(db_path: Path | None = None, adapter: ModelAdapter | None = None,
               decision_timeout: float = 30, web_path: Path | None = None,
               reading_scale: float = 1, default_playback: Literal["manual", "auto"] = "manual",
               qualification_factory: AdapterFactory | None = None,
               qualification_timeout: float = 35, qualification_retry_delay: float = 1,
               game_factory: AdapterFactory | None = None, game_retry_delay: float = 1,
               qualification_enabled: bool = False, opening_enabled: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = db_path or Path(os.getenv("WEREWOLF_DB_PATH") or
                               str(Path(__file__).resolve().parents[2] / "data" / "werewolf.sqlite3"))
        app.state.service = GameService(path, adapter, decision_timeout, reading_scale, opening_enabled)
        try:
            app.state.accounts = AccountStore(path.with_name(f"{path.stem}-accounts.sqlite3"))
            app.state.qualifications = QualificationService(app.state.accounts, qualification_factory,
                                                           timeout=qualification_timeout,
                                                           retry_delay=qualification_retry_delay,
                                                           enabled=qualification_enabled)
            app.state.qualifications.opening_policy = app.state.service.opening_policy
            app.state.service.real_gateway = RealGameGateway(app.state.accounts, app.state.qualifications, game_factory,
                                                            opening_policy=app.state.service.opening_policy)
            app.state.qualification_batches = QualificationBatches(app.state.qualifications)
            app.state.service.retry_delay = game_retry_delay
            yield
        finally:
            await app.state.service.close()
            if hasattr(app.state, "qualification_batches"):
                await app.state.qualification_batches.close()
            if hasattr(app.state, "qualifications"):
                await app.state.qualifications.close()
            if hasattr(app.state, "accounts"):
                app.state.accounts.close()

    app = FastAPI(title="LLM Werewolf — local API", version="0.4.0", lifespan=lifespan)

    @app.middleware("http")
    async def account_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/account"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/account"):
            # FastAPI's default validation detail can echo raw secret input.
            return JSONResponse({"detail": "输入不符合要求，请检查用户名、密码或连接参数"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    app.include_router(account_router)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    def session(request: Request, game_id: str):
        authorization = request.headers.get("authorization", "")
        token = (authorization[7:] if authorization.startswith("Bearer ") else None) if authorization else (
            request.cookies.get("werewolf_session")
        )
        service = request.app.state.service
        return service, service.authorize(game_id, token)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": "mock_and_models", "database_version": Store.SCHEMA_VERSION,
                "connected_providers": ["deepseek", "siliconflow", "dashscope"],
                "siliconflow_models": ["tencent/Hy4-preview", "zai-org/GLM-5.3"],
                "qualification_required": app.state.qualifications.enabled,
                "new_game_limits": {**asdict(DEFAULT_PHASE_LIMITS), "reasoning_budget_provider":"siliconflow",
                                    "thinking_mode":"requested_disabled", "decision_budget_policy":asdict(V10_BUDGET_POLICY)}}

    @app.post("/api/games", response_model=CreatedGame, status_code=201)
    async def create_game(body: CreateGame, request: Request, response: Response):
        visible, token = await request.app.state.service.create(
            body.seed, body.human_seat, body.connections, body.ai_profiles, body.playback_mode or default_playback,
            body.roster.engine_config()
        )
        response.set_cookie("werewolf_session", token, httponly=True, samesite="strict",
                            path=f"/api/games/{visible['game_id']}")
        return {"game": visible, "session_token": token}

    @app.get("/api/games/{game_id}", response_model=GameView)
    async def view(game_id: str, request: Request):
        _, runtime = session(request, game_id)
        async with runtime.lock:
            return runtime.visible()

    @app.post("/api/games/{game_id}/session", response_model=GameView)
    async def recover_session(game_id: str, body: SessionInput, request: Request, response: Response):
        runtime = request.app.state.service.authorize(game_id, body.token)
        response.set_cookie("werewolf_session", body.token, httponly=True, samesite="strict",
                            path=f"/api/games/{game_id}")
        async with runtime.lock:
            return runtime.visible()

    @app.post("/api/games/{game_id}/actions", response_model=GameView)
    async def action(game_id: str, body: SubmitAction, request: Request):
        service, runtime = session(request, game_id)
        return await service.submit(runtime, body.operation_id, body.expected_version, Action(**body.action.model_dump()))

    @app.post("/api/games/{game_id}/pause", response_model=GameView)
    async def pause(game_id: str, body: ControlInput, request: Request):
        service, runtime = session(request, game_id)
        return await service.control(runtime, body.operation_id, "pause")

    @app.post("/api/games/{game_id}/resume", response_model=GameView)
    async def resume(game_id: str, body: ResumeInput, request: Request):
        service, runtime = session(request, game_id)
        return await service.control(runtime, body.operation_id, "resume", body.recovery)

    @app.post("/api/games/{game_id}/advance", response_model=GameView)
    async def advance(game_id: str, body: AdvanceInput, request: Request):
        service, runtime = session(request, game_id)
        return await service.advance(runtime, body.operation_id, body.expected_step)

    @app.post("/api/games/{game_id}/playback", response_model=GameView)
    async def playback(game_id: str, body: PlaybackInput, request: Request):
        service, runtime = session(request, game_id)
        return await service.set_playback(runtime, body.operation_id, body.mode)

    @app.post("/api/games/{game_id}/abandon", response_model=GameView)
    async def abandon(game_id: str, body: ControlInput, request: Request):
        service, runtime = session(request, game_id)
        return await service.control(runtime, body.operation_id, "abandon")

    @app.get("/api/games/{game_id}/usage")
    async def game_usage(game_id: str, request: Request):
        service, runtime = session(request, game_id)
        async with runtime.lock:
            # Aggregate only during play: per-attempt night actors/actions would leak secrets.
            return {"mode": runtime.mode, "simulated_transport": runtime.simulated_transport,
                    **usage_summary(service.store.attempts(game_id))}

    @app.get("/api/games/{game_id}/events", response_model=EventBatch)
    async def events(game_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0):
        service, runtime = session(request, game_id)
        packets, cursor, _ = await service.events(runtime, after)
        return {"events": packets, "cursor": cursor}

    @app.get("/api/games/{game_id}/stream")
    async def stream(game_id: str, request: Request, after: Annotated[int | None, Query(ge=0)] = None,
                     follow: bool = True, last_event_id: Annotated[str | None, Header()] = None):
        service, runtime = session(request, game_id)
        try:
            cursor = after if after is not None else int(last_event_id or "0")
        except ValueError as exc:
            raise ServiceError(422, "事件游标必须是非负整数") from exc
        if cursor < 0:
            raise ServiceError(422, "事件游标必须是非负整数")
        await service.events(runtime, cursor)

        async def generate():
            nonlocal cursor
            while True:
                packets, _, signal = await service.events(runtime, cursor)
                for packet in packets:
                    cursor = packet["id"]
                    yield f"id: {cursor}\nevent: state\ndata: {canonical_json(packet['state'])}\n\n"
                if not follow or runtime.status in {"finished", "abandoned"} or await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(signal.wait(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/games/{game_id}/replay")
    async def replay(game_id: str, request: Request):
        service, runtime = session(request, game_id)
        async with runtime.lock:
            if not runtime.engine.finished:
                raise ServiceError(409, "完整复盘仅在对局结束后开放")
            attempts = service.store.attempts(game_id)
            if runtime.player_prompt_version == PLAYER_PROMPT_VERSION:
                # Post-game role reveal does not publish subjective memories or opening intentions.
                attempts = [{k: v for k, v in attempt.items() if k != "player_update"} for attempt in attempts]
            return {"mode": runtime.mode, **runtime.engine.replay(), "usage": attempts}

    assets = web_path if web_path is not None else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (assets / "index.html").is_file():
        # Register last so /api and /docs always take priority over the built webpage.
        app.mount("/", StaticFiles(directory=assets, html=True), name="web")

    return app


app = create_app()
