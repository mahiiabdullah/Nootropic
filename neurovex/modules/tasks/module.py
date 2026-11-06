"""Tasks module lifecycle: the store, live events and the reminder loop.

Reminders are delivered to open Neurovex pages over SSE (toast + desktop
notification). A reminder that comes due while no page is open waits until one
connects, so it's late rather than lost. Native notifications arrive with the
desktop shell (Phase 3); the loop moves onto the shared scheduler in Phase 6.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.core.live import Broadcaster
from neurovex.core.logging import get_logger
from neurovex.modules.tasks.models import Task
from neurovex.modules.tasks.planner import PRIORITY_NAMES, due_label
from neurovex.modules.tasks.store import TaskStore

log = get_logger(__name__)


class TasksModule:
    module_id = "tasks"

    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self.live = Broadcaster()
        self.store = TaskStore(sessions, settings.timezone, on_change=self._on_change)
        self._task: asyncio.Task[None] | None = None

    def _on_change(self, action: str, task: Task) -> None:
        self.live.publish("task", {"action": action, "id": task.id, "title": task.title})

    async def start(self) -> None:
        self._task = asyncio.create_task(self._reminder_loop(), name="tasks-reminders")
        log.info("tasks_module_started", tz=str(self.store.tz))

    async def stop(self) -> None:
        self.live.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def check_reminders(self) -> int:
        """Publish reminders that are due. Returns how many were delivered."""
        if not self.live.subscriber_count:
            return 0  # nobody to tell yet: keep them for when a page connects
        now = utcnow()
        due = await self.store.due_reminders(now)
        local_now = now.astimezone(self.store.tz)
        for t in due:
            self.live.publish(
                "reminder",
                {
                    "id": t.id,
                    "title": t.title,
                    "due_label": due_label(t, local_now, self.store.tz),
                    "priority": PRIORITY_NAMES.get(t.priority, "normal"),
                    "late_min": max(0, int((now - t.remind_at).total_seconds() // 60)),  # type: ignore[operator]
                },
            )
        await self.store.mark_reminded([t.id for t in due], now)
        if due:
            log.info("tasks_reminded", count=len(due))
        return len(due)

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await self.check_reminders()
            except Exception:
                log.exception("tasks_reminder_check_failed")
            await asyncio.sleep(self.settings.tasks_reminder_check_s)
