"""Task storage: add, list, change, complete (with repeats), reminders. Local, no AI."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, tzinfo
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.clock import local_tz
from neurovex.core.db import utcnow
from neurovex.modules.tasks.dates import (
    REPEATS,
    Due,
    at_time,
    day_end,
    next_due,
    parse_due,
    parse_quick,
)
from neurovex.modules.tasks.models import Task, TaskStatus
from neurovex.modules.tasks.planner import UPCOMING_DAYS

MAX_TITLE = 200
MAX_NOTES = 2000
VIEWS = ("today", "upcoming", "open", "overdue", "someday", "done", "all")
_UNSET: Any = object()
_FILLER = {"the", "a", "an", "my", "to", "for", "of", "task", "todo"}

OnChange = Callable[[str, Task], None]


class TaskError(ValueError):
    """A request the store can't carry out. The message is safe to show people and models."""


def clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:MAX_TITLE]


def _clean_notes(text: str | None) -> str | None:
    return (text or "").strip()[:MAX_NOTES] or None


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    out = (re.sub(r"[^\w-]", "", t.lower().lstrip("#"))[:30] for t in tags or [])
    return list(dict.fromkeys(t for t in out if t))[:8]


def _check(priority: int | None = None, repeat: str | None = None) -> None:
    if priority is not None and priority not in (1, 2, 3, 4):
        raise TaskError("Priority must be 1 (low) to 4 (urgent).")
    if repeat and repeat not in REPEATS:
        raise TaskError(f"Repeat must be one of {', '.join(REPEATS)}.")


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class TaskStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tz_name: str = "",
        on_change: OnChange | None = None,
    ) -> None:
        self.sessions = sessions
        self.tz_name = tz_name
        self.on_change = on_change

    @property
    def tz(self) -> tzinfo:
        return local_tz(self.tz_name)

    def local_now(self) -> datetime:
        return utcnow().astimezone(self.tz)

    def end_of_today(self) -> datetime:
        return day_end(self.local_now().date(), self.tz)

    def parse_due(self, text: str | None) -> Due | None:
        """None for empty text; TaskError when there is text but no date in it."""
        if not (text or "").strip():
            return None
        due = parse_due(text or "", self.local_now())
        if due is None:
            raise TaskError(
                f"Couldn't read the date '{text}'. Try 'tomorrow 5pm', 'fri' or '2026-10-20'."
            )
        return due

    def _changed(self, action: str, task: Task) -> None:
        if self.on_change is not None:
            self.on_change(action, task)

    # --- create -----------------------------------------------------------------------

    async def add(
        self,
        title: str,
        *,
        due: Due | None = None,
        priority: int = 2,
        notes: str | None = None,
        tags: Iterable[str] | None = None,
        repeat: str | None = None,
        estimate_min: int | None = None,
        remind_at: datetime | None = None,
        source: str = "user",
    ) -> Task:
        title = clean_title(title)
        if not title:
            raise TaskError("A task needs a title.")
        _check(priority, repeat)
        if repeat and due is None:  # a repeat needs a first date: today
            due = Due(self.end_of_today(), all_day=True)
        if remind_at is None and due is not None and not due.all_day:
            remind_at = due.at  # timed tasks remind you when they're due
        task = Task(
            title=title,
            notes=_clean_notes(notes),
            priority=priority,
            due_at=due.at if due else None,
            all_day=due.all_day if due else True,
            remind_at=remind_at,
            repeat=repeat or None,
            tags=_clean_tags(tags),
            estimate_min=estimate_min or None,
            source=source,
        )
        async with self.sessions() as s:
            s.add(task)
            await s.commit()
        self._changed("added", task)
        return task

    async def quick_add(self, text: str, source: str = "user") -> Task:
        """Add from one line of text: "Call bank tomorrow 10am !high #errands ~15m"."""
        p = parse_quick(text, self.local_now())
        return await self.add(
            p.title,
            due=p.due,
            priority=p.priority or 2,
            tags=p.tags,
            repeat=p.repeat,
            estimate_min=p.estimate_min,
            source=source,
        )

    # --- read ---------------------------------------------------------------------------

    async def get(self, task_id: int) -> Task | None:
        async with self.sessions() as s:
            return await s.get(Task, task_id)

    async def list(self, view: str = "open", query: str = "", limit: int = 200) -> list[Task]:
        if view not in VIEWS:
            raise TaskError(f"View must be one of {', '.join(VIEWS)}.")
        now, end_today = utcnow(), self.end_of_today()
        stmt = select(Task)
        if view == "done":
            stmt = stmt.where(Task.status == TaskStatus.DONE).order_by(Task.completed_at.desc())
        else:
            if view != "all":
                stmt = stmt.where(Task.status == TaskStatus.OPEN)
            if view == "today":  # includes overdue
                stmt = stmt.where(Task.due_at <= end_today)
            elif view == "overdue":
                stmt = stmt.where(Task.due_at < now)
            elif view == "upcoming":
                stmt = stmt.where(
                    Task.due_at > end_today,
                    Task.due_at <= end_today + timedelta(days=UPCOMING_DAYS),
                )
            elif view == "someday":
                stmt = stmt.where(Task.due_at.is_(None))
            stmt = stmt.order_by(Task.due_at.is_(None), Task.due_at, Task.priority.desc(), Task.id)
        words = [w for w in query.lower().split() if w not in _FILLER][:6]
        for w in words:
            stmt = stmt.where(Task.title.ilike(_like(w), escape="\\"))
        async with self.sessions() as s:
            return list(await s.scalars(stmt.limit(limit)))

    async def resolve(self, ref: str | int) -> Task:
        """A task from its id ("12", "#12") or words from its title (open tasks only)."""
        raw = str(ref).strip().lstrip("#")
        if raw.isdigit():
            task = await self.get(int(raw))
            if task is None:
                raise TaskError(f"No task #{raw}.")
            return task
        matches = await self.list("open", raw, limit=6)
        if not matches:
            raise TaskError(f"No open task matches '{raw}'.")
        exact = [t for t in matches if t.title.lower() == raw.lower()]
        if len(matches) == 1 or len(exact) == 1:
            return exact[0] if exact else matches[0]
        names = "; ".join(f"#{t.id} {t.title}" for t in matches)
        raise TaskError(f"Several open tasks match '{raw}': {names}. Use the id.")

    async def done_today(self) -> list[Task]:
        start = self.local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(Task)
                    .where(Task.status == TaskStatus.DONE, Task.completed_at >= start)
                    .order_by(Task.completed_at.desc())
                    .limit(50)
                )
            )

    async def counts(self) -> dict[str, int]:
        now, end_today = utcnow(), self.end_of_today()
        async with self.sessions() as s:
            overdue, today, total = (
                await s.execute(
                    select(
                        func.count().filter(Task.due_at < now),
                        func.count().filter(Task.due_at >= now, Task.due_at <= end_today),
                        func.count(),
                    ).where(Task.status == TaskStatus.OPEN)
                )
            ).one()
        return {"overdue": overdue, "today": today, "open": total}

    # --- change -------------------------------------------------------------------------

    async def update(
        self,
        task_id: int,
        *,
        title: str = _UNSET,
        notes: str | None = _UNSET,
        due: Due | None = _UNSET,
        priority: int = _UNSET,
        tags: Iterable[str] | None = _UNSET,
        repeat: str | None = _UNSET,
        estimate_min: int | None = _UNSET,
        remind_at: datetime | None = _UNSET,
    ) -> Task:
        async with self.sessions() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise TaskError(f"No task #{task_id}.")
            if title is not _UNSET:
                if not (cleaned := clean_title(title)):
                    raise TaskError("A task needs a title.")
                task.title = cleaned
            if notes is not _UNSET:
                task.notes = _clean_notes(notes)
            if priority is not _UNSET:
                _check(priority=priority)
                task.priority = priority
            if tags is not _UNSET:
                task.tags = _clean_tags(tags)
            if estimate_min is not _UNSET:
                task.estimate_min = estimate_min or None
            if repeat is not _UNSET:
                _check(repeat=repeat)
                task.repeat = repeat or None
            if due is not _UNSET:
                old_due, old_remind = task.due_at, task.remind_at
                if old_due is not None and due is not None and due.at > old_due:
                    task.deferrals += 1
                task.due_at = due.at if due else None
                task.all_day = due.all_day if due else True
                if remind_at is _UNSET and old_remind in (None, old_due):
                    # The reminder followed the due time: keep it in step.
                    remind_at = due.at if due is not None and not due.all_day else None
            if task.repeat and task.due_at is None:
                task.due_at, task.all_day = self.end_of_today(), True
            if remind_at is not _UNSET and remind_at != task.remind_at:
                task.remind_at, task.reminded_at = remind_at, None
            task.updated_at = utcnow()
            await s.commit()
        self._changed("updated", task)
        return task

    async def postpone(self, task_id: int, days: int = 1) -> Task:
        """Push to `days` after today (or after its due day, if that's later); keeps the time."""
        task = await self.get(task_id)
        if task is None:
            raise TaskError(f"No task #{task_id}.")
        tz, today = self.tz, self.local_now().date()
        base = max(task.due_at.astimezone(tz).date(), today) if task.due_at else today
        day = base + timedelta(days=days)
        if task.due_at is None or task.all_day:
            due = Due(day_end(day, tz), all_day=True)
        else:
            due = Due(at_time(day, task.due_at.astimezone(tz).time(), tz), all_day=False)
        return await self.update(task_id, due=due)

    async def snooze(self, task_id: int, minutes: int = 10) -> Task:
        return await self.update(task_id, remind_at=utcnow() + timedelta(minutes=minutes))

    async def complete(self, task_id: int) -> tuple[Task, Task | None]:
        """Mark done. A repeating task also gets its next occurrence, returned second."""
        nxt = None
        async with self.sessions() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise TaskError(f"No task #{task_id}.")
            if task.status == TaskStatus.DONE:
                return task, None
            now = utcnow()
            task.status, task.completed_at, task.updated_at = TaskStatus.DONE, now, now
            if task.repeat and task.due_at is not None:
                at = next_due(task.due_at, task.repeat, self.tz, now)
                shift = at - task.due_at
                nxt = Task(
                    title=task.title,
                    notes=task.notes,
                    priority=task.priority,
                    due_at=at,
                    all_day=task.all_day,
                    remind_at=task.remind_at + shift if task.remind_at else None,
                    repeat=task.repeat,
                    tags=list(task.tags or []),
                    estimate_min=task.estimate_min,
                    source=task.source,
                    created_at=now,
                    updated_at=now,  # equal stamps = untouched, which reopen() relies on
                )
                s.add(nxt)
            await s.commit()
        self._changed("completed", task)
        if nxt is not None:
            self._changed("added", nxt)
        return task, nxt

    async def reopen(self, task_id: int) -> Task:
        """Undo complete/cancel. For a repeat, the untouched next occurrence is removed."""
        async with self.sessions() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise TaskError(f"No task #{task_id}.")
            if task.repeat and task.completed_at is not None:
                await s.execute(
                    delete(Task).where(
                        Task.status == TaskStatus.OPEN,
                        Task.title == task.title,
                        Task.repeat == task.repeat,
                        Task.created_at >= task.completed_at - timedelta(seconds=1),
                        Task.updated_at == Task.created_at,
                    )
                )
            task.status, task.completed_at, task.updated_at = TaskStatus.OPEN, None, utcnow()
            await s.commit()
        self._changed("updated", task)
        return task

    async def cancel(self, task_id: int) -> Task:
        async with self.sessions() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise TaskError(f"No task #{task_id}.")
            now = utcnow()
            task.status, task.completed_at, task.updated_at = TaskStatus.CANCELLED, now, now
            await s.commit()
        self._changed("cancelled", task)
        return task

    async def delete(self, task_id: int) -> bool:
        task = await self.get(task_id)
        if task is None:
            return False
        async with self.sessions() as s:
            await s.execute(delete(Task).where(Task.id == task_id))
            await s.commit()
        self._changed("deleted", task)
        return True

    # --- reminders ----------------------------------------------------------------------

    async def due_reminders(self, now: datetime) -> list[Task]:
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.OPEN,
                        Task.remind_at <= now,
                        Task.reminded_at.is_(None),
                    )
                    .order_by(Task.remind_at)
                    .limit(20)
                )
            )

    async def mark_reminded(self, ids: list[int], at: datetime) -> None:
        if not ids:
            return
        async with self.sessions() as s:
            await s.execute(update(Task).where(Task.id.in_(ids)).values(reminded_at=at))
            await s.commit()
