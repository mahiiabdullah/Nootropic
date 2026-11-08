"""FastAPI application: `uv run nvx serve`, then open http://127.0.0.1:8765."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

from neurovex import __version__
from neurovex.api.auth import AuthMiddleware
from neurovex.api.auth import router as auth_router
from neurovex.api.guard import LocalGuardMiddleware
from neurovex.api.system import router as system_router
from neurovex.conversation.api import router as chat_router
from neurovex.conversation.builtins import builtin_skills
from neurovex.conversation.engine import ChatEngine
from neurovex.core.backup import BackupModule
from neurovex.core.config import Settings, get_settings
from neurovex.core.db import create_engine, create_sessionmaker, init_db
from neurovex.core.live import sse_stream
from neurovex.core.logging import configure_logging, get_logger
from neurovex.core.modules import ModuleManager
from neurovex.core.skills import SkillRegistry
from neurovex.llm.api import router as llm_router_api
from neurovex.llm.router import LLMRouter
from neurovex.llm.usage import db_recorder
from neurovex.memory.api import router as memory_router
from neurovex.memory.skills import MANIFEST as MEMORY_MANIFEST
from neurovex.memory.skills import memory_skills
from neurovex.memory.store import MemoryStore
from neurovex.modules.news.api import router as news_router
from neurovex.modules.news.module import NewsModule
from neurovex.modules.news.skills import NEWS_HINTS, news_skills
from neurovex.modules.news.summarizer import SummarizerFactory, router_summarizer_factory
from neurovex.modules.tasks.api import router as tasks_router
from neurovex.modules.tasks.module import TasksModule
from neurovex.modules.tasks.skills import TASK_HINTS, tasks_skills
from neurovex.modules.tasks.store import TaskStore
from neurovex.routines.api import router as routines_router
from neurovex.routines.skills import MANIFEST as ROUTINES_MANIFEST
from neurovex.routines.skills import ROUTINE_HINTS, routine_skills
from neurovex.routines.store import RoutineStore
from neurovex.security.audit import AuditLog
from neurovex.security.auth import AuthService
from neurovex.security.permissions import PermissionStore
from neurovex.today.api import router as today_router
from neurovex.today.brief import TodayBrief
from neurovex.today.module import TodayModule
from neurovex.today.skills import TODAY_HINTS, today_skills

WEB_DIR = Path(__file__).parent / "web"
log = get_logger("neurovex")


def build_chat(
    router: LLMRouter,
    news: NewsModule,
    sessions,  # type: ignore[no-untyped-def]
    tasks: TaskStore | None = None,
    audit: AuditLog | None = None,
    allowed: Callable[[str], bool] | None = None,
) -> ChatEngine:
    """Assemble memory, the skill registry from every module, and the chat engine.
    `allowed` is the permission check (Settings → What chat may do); None allows everything."""
    tz = news.settings.timezone
    memory = MemoryStore(sessions)
    skills = SkillRegistry(audit=audit.append if audit else None, allowed=allowed)
    skills.register_all(builtin_skills(router, tz))
    skills.register_all(memory_skills(memory))
    skills.register_all(news_skills(news))
    task_store = tasks or TaskStore(sessions, tz)
    skills.register_all(tasks_skills(task_store))
    skills.register_all(today_skills(TodayBrief(sessions, task_store, memory)))
    routines = RoutineStore(sessions)
    skills.register_all(routine_skills(routines, skills))  # last: it checks steps against the rest
    skills.hint("news", NEWS_HINTS)
    skills.hint("tasks", TASK_HINTS)
    skills.hint("routines", ROUTINE_HINTS)
    skills.hint("today", TODAY_HINTS)
    return ChatEngine(router, skills, sessions, memory, tz, allowed, routines)


def create_app(
    settings: Settings | None = None,
    summarizer_factory: SummarizerFactory | None = None,
    llm: LLMRouter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        configure_logging(settings)
        engine = create_engine(settings.db_url)
        await init_db(engine)
        sessions = create_sessionmaker(engine)
        app.state.audit = AuditLog(sessions)
        app.state.auth = AuthService(settings, sessions)
        app.state.auth.token()  # made on first start, so `nvx token` and scripts can use it
        # One router for the whole app: cooldowns and health persist across requests.
        router = llm or LLMRouter.from_settings(settings, db_recorder(sessions))
        app.state.llm = router
        log.info("llm_providers", configured=router.configured(), strategy=settings.llm_strategy)
        modules = ModuleManager()
        app.state.modules = modules
        news = NewsModule(
            settings, sessions, summarizer_factory or router_summarizer_factory(router)
        )
        app.state.news = news
        modules.add(news)
        tasks = TasksModule(settings, sessions)
        app.state.tasks = tasks
        modules.add(tasks)
        modules.declare(MEMORY_MANIFEST)
        modules.declare(ROUTINES_MANIFEST)
        permissions = PermissionStore(sessions)
        app.state.permissions = permissions
        app.state.chat = build_chat(
            router, news, sessions, tasks.store, app.state.audit, permissions.allowed
        )
        app.state.memory = app.state.chat.memory
        today = TodayModule(settings, TodayBrief(sessions, tasks.store, app.state.memory))
        app.state.today = today
        modules.add(today)
        modules.add(BackupModule(settings))
        await permissions.load(modules.permissions())
        await modules.start_all()  # a module that fails is marked degraded; the rest run
        log.info("neurovex_ready", version=__version__, profile=settings.profile.value,
                 url=f"http://{settings.host}:{settings.port}")  # fmt: skip
        try:
            yield
        finally:
            await modules.stop_all()
            await router.aclose()
            await engine.dispose()
            log.info("neurovex_stopped")

    app = FastAPI(title="Neurovex", version=__version__, lifespan=lifespan)
    app.add_middleware(AuthMiddleware)  # innermost: a bad Host is refused before sign-in is checked
    app.add_middleware(
        LocalGuardMiddleware, allow_remote=settings.allow_remote, extra_hosts=settings.extra_hosts()
    )
    # Pages and JSON shrink by about three quarters; the live-event stream is never compressed.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.include_router(auth_router)
    app.include_router(system_router)
    app.include_router(news_router)
    app.include_router(llm_router_api)
    app.include_router(chat_router)
    app.include_router(memory_router)
    app.include_router(tasks_router)
    app.include_router(today_router)
    app.include_router(routines_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        news: NewsModule = app.state.news
        return {
            "status": "ok",
            "version": __version__,
            "profile": settings.profile.value,
            "uptime_s": round(time.monotonic() - started, 1),
            "modules": {"news": {"provider": news.pipeline.provider_name,
                                 "running": news.pipeline.running},
                        "tasks": await app.state.tasks.store.counts()},
            "llm": {"configured": app.state.llm.configured()},
            "degraded": [m["id"] for m in app.state.modules.status() if m["state"] == "degraded"],
        }  # fmt: skip

    @app.get("/api/events", tags=["system"])
    async def events(request: Request) -> StreamingResponse:
        """Live events from every module on one stream: one connection per open page."""
        return StreamingResponse(
            sse_stream(
                [app.state.news.live, app.state.tasks.live, app.state.today.live],
                request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        # The browser asks each time and gets "unchanged" unless the page was edited.
        path = WEB_DIR / "index.html"
        st = path.stat()
        headers = {"ETag": f'"{st.st_mtime_ns:x}-{st.st_size:x}"', "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return FileResponse(path, headers=headers)

    return app
