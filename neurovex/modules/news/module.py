"""News module lifecycle: owns the HTTP client, the pipeline, live events and the refresh loop.

The refresh loop is a plain asyncio task for now; it moves onto the shared
APScheduler in Phase 6 (docs/PROMPTS.md P6.1).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.core.live import Broadcaster
from neurovex.core.logging import get_logger
from neurovex.modules.news import fetcher, store
from neurovex.modules.news.pipeline import AlreadyRunning, NewsPipeline
from neurovex.modules.news.summarizer import SummarizerFactory

log = get_logger(__name__)


class NewsModule:
    module_id = "news"

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        summarizer_factory: SummarizerFactory,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.http = fetcher.build_http_client()
        self.live = Broadcaster()
        self.pipeline = NewsPipeline(settings, sessions, self.http, summarizer_factory, self.live)
        self.next_run_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[object]] = set()

    async def start(self) -> None:
        async with self.sessions() as s:
            seeded = await store.seed_default_sources(s)
            await s.commit()
        if seeded:
            log.info("news_sources_seeded", count=seeded)
        await self.pipeline.detect_provider()
        if self.settings.news_auto_refresh:
            self._task = asyncio.create_task(self._refresh_loop(), name="news-refresh")
        log.info(
            "news_module_started",
            provider=self.pipeline.provider_name,
            auto_refresh=self.settings.news_auto_refresh,
        )

    async def stop(self) -> None:
        self.live.close()  # end open browser streams
        tasks = [t for t in [self._task, *self._background] if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self.http.aclose()

    def _spawn(self, coro: Awaitable[object]) -> None:
        task: asyncio.Task[object] = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def trigger_refresh(self) -> bool:
        """Start a background run. Returns False if one is already running."""
        if self.pipeline.running:
            return False
        self._spawn(self._run_quietly("manual"))
        return True

    def trigger_briefing(self) -> bool:
        """Start writing a briefing in the background. False if one is in progress."""
        if self.pipeline.briefing_running:
            return False
        self.pipeline.briefing_running = True  # claim it before the task starts
        self._spawn(self._brief_quietly())
        return True

    async def _brief_quietly(self) -> object:
        try:
            return await self.pipeline.make_briefing("manual")
        except Exception as e:
            log.warning("news_briefing_failed", error=str(e)[:300])
            return None
        finally:
            self.pipeline.briefing_running = False

    async def _run_quietly(self, trigger: str) -> object:
        try:
            return await self.pipeline.run(trigger)
        except AlreadyRunning:
            return None

    async def _refresh_loop(self) -> None:
        interval = timedelta(minutes=self.settings.news_refresh_minutes)
        self.next_run_at = utcnow() + timedelta(seconds=self.settings.news_first_refresh_delay_s)
        await asyncio.sleep(self.settings.news_first_refresh_delay_s)
        while True:
            try:
                await self.pipeline.run("schedule")
            except AlreadyRunning:
                pass
            except Exception:
                log.exception("news_scheduled_run_failed")
            self.next_run_at = utcnow() + interval
            await asyncio.sleep(interval.total_seconds())
