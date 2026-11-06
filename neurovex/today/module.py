"""The morning announcement: once a day, tell open pages that today's brief is ready.

Nothing is sent to any AI and nothing is lost if no page is open: Today is always there
when you open it. A brief still unannounced hours later is skipped rather than sent late
in the day. The loop moves onto the shared scheduler in Phase 6.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, time, timedelta

from sqlalchemy.exc import IntegrityError

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.core.live import Broadcaster
from neurovex.core.logging import get_logger
from neurovex.today.brief import TodayBrief
from neurovex.today.models import DailyBrief

log = get_logger(__name__)

CHECK_S = 30
ANNOUNCE_WINDOW = timedelta(hours=6)


def in_window(now_local: datetime, brief_time: str) -> bool:
    """Is it time to announce? `brief_time` is "HH:MM"; empty turns the announcement off."""
    if not brief_time:
        return False
    hour, minute = (int(x) for x in brief_time.split(":"))
    start = datetime.combine(now_local.date(), time(hour, minute), now_local.tzinfo)
    return start <= now_local < start + ANNOUNCE_WINDOW


class TodayModule:
    module_id = "today"

    def __init__(self, settings: Settings, brief: TodayBrief) -> None:
        self.settings = settings
        self.brief = brief
        self.live = Broadcaster()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="today-morning")

    async def stop(self) -> None:
        self.live.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def check_morning(self, now: datetime | None = None) -> bool:
        """Announce today's brief if it's due. Returns True if it was announced now."""
        if not self.live.subscriber_count:
            return False  # nobody to tell yet
        local = (now or utcnow()).astimezone(self.brief.tasks.tz)
        if not in_window(local, self.settings.today_brief_time):
            return False
        day = local.date().isoformat()
        async with self.brief.sessions() as s:
            if await s.get(DailyBrief, day) is not None:
                return False
        today = await self.brief.build()
        try:
            async with self.brief.sessions() as s:
                s.add(DailyBrief(day=day, line=today.line[:400]))
                await s.commit()
        except IntegrityError:  # another server process got there first
            return False
        self.live.publish(
            "today", {"greeting": today.greeting, "date": today.date_label, "line": today.line}
        )
        log.info("today_announced", day=day)
        return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_morning()
            except Exception:
                log.exception("today_check_failed")
            await asyncio.sleep(CHECK_S)
