"""Build the day's overview. Everything here is local: no AI calls, so it's instant and free.

The one AI-written piece, the personal news briefing, is reused if it's recent; it's
written by the news pipeline on its own schedule, never because Today was opened.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.db import utcnow
from neurovex.memory.models import Memory, MemoryKind
from neurovex.memory.store import MemoryStore
from neurovex.modules.news import store as news_store
from neurovex.modules.news.briefing import briefing_out
from neurovex.modules.news.schemas import ArticleOut, BriefingOut
from neurovex.modules.tasks.api import SuggestionOut, TaskOut
from neurovex.modules.tasks.models import Task
from neurovex.modules.tasks.planner import bucket, describe, suggest
from neurovex.modules.tasks.store import TaskStore

STORY_COUNT = 5
STORY_WINDOWS_H = (24, 72)  # a quiet day falls back to the last three days
BRIEFING_MAX_AGE = timedelta(hours=24)
MAX_GOALS = 4
# Case-insensitive except the name itself, which must be capitalized ("called back" isn't one).
_NAME = re.compile(r"\b(?i:name is|call (?:me|them)|called|goes by)\s+([A-Z][\w'-]+)")


class TodayOut(BaseModel):
    part_of_day: str  # morning | afternoon | evening
    greeting: str
    date_label: str
    line: str  # the whole day in a sentence or two
    next: SuggestionOut | None
    overdue: list[TaskOut]
    today: list[TaskOut]
    tomorrow: list[TaskOut]
    done_today: list[TaskOut]
    counts: dict[str, int]
    stories: list[ArticleOut]
    stories_window_hours: int
    new_stories: int
    briefing: BriefingOut | None
    goals: list[str]


def part_of_day(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "morning"
    return "afternoon" if 12 <= now.hour < 17 else "evening"


def name_from(memories: list[Memory]) -> str | None:
    """The user's name, if a memory states it ("Name is Sam", "Call me Sam")."""
    for m in memories:
        if found := _NAME.search(m.text):
            return found.group(1)
    return None


def greeting(now: datetime, name: str | None = None) -> str:
    return f"Good {part_of_day(now)}" + (f", {name}" if name else "")


def date_label(now: datetime) -> str:
    return f"{now.strftime('%A')} {now.day} {now.strftime('%B')}"


def summary_line(
    overdue: int, due_today: int, done: int, next_title: str | None, new_stories: int
) -> str:
    """The day in a sentence or two, the same for the UI, the CLI and notifications."""
    parts = []
    if done:
        parts.append(f"You've finished {done} today.")
    if overdue and due_today:
        parts.append(f"{overdue} overdue and {due_today} still due today.")
    elif overdue:
        parts.append(f"{overdue} overdue.")
    elif due_today:
        parts.append(f"{due_today} still due today.")
    elif not done:
        parts.append("Nothing due today.")
    if next_title:
        parts.append(
            f"Start with {next_title}." if overdue or due_today else f"Next up: {next_title}."
        )
    if new_stories:
        parts.append(
            f"{new_stories} new {'story' if new_stories == 1 else 'stories'} since your last visit."
        )
    return " ".join(parts)


class TodayBrief:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], tasks: TaskStore, memory: MemoryStore
    ) -> None:
        self.sessions = sessions
        self.tasks = tasks
        self.memory = memory

    async def build(self) -> TodayOut:
        now, tz = utcnow(), self.tasks.tz
        local = now.astimezone(tz)
        open_tasks = await self.tasks.list("open", limit=500)
        done = await self.tasks.done_today()

        def out(t: Task) -> TaskOut:
            return TaskOut(**describe(t, now, tz))

        def in_bucket(name: str) -> list[TaskOut]:
            return [out(t) for t in open_tasks if bucket(t, now, tz) == name]

        tomorrow = local.date() + timedelta(days=1)
        due_tomorrow = [
            out(t)
            for t in open_tasks
            if t.due_at is not None and t.due_at.astimezone(tz).date() == tomorrow
        ]
        overdue, due_today = in_bucket("overdue"), in_bucket("today")
        picks = suggest(open_tasks, now, tz, limit=1)
        pick = picks[0] if picks else None

        async with self.sessions() as s:
            profile = await news_store.load_profile(s)
            stories: list[ArticleOut] = []
            window = STORY_WINDOWS_H[0]
            for window in STORY_WINDOWS_H:
                stories = await news_store.digest(s, profile, hours=window, limit=STORY_COUNT)
                if stories:
                    break
            new_stories = await news_store.count_new_since(s, await news_store.get_last_seen(s))
            latest = await news_store.latest_briefing(s)
            recent = latest is not None and now - latest.created_at <= BRIEFING_MAX_AGE
            written = await briefing_out(s, latest) if recent and latest else None

        goals = await self.memory.list(kind=MemoryKind.GOAL.value, limit=MAX_GOALS)
        name = name_from(await self.memory.list(limit=200))
        return TodayOut(
            part_of_day=part_of_day(local),
            greeting=greeting(local, name),
            date_label=date_label(local),
            line=summary_line(
                len(overdue),
                len(due_today),
                len(done),
                pick.task.title if pick else None,
                new_stories,
            ),
            next=(
                SuggestionOut(task=out(pick.task), score=pick.score, reasons=pick.reasons)
                if pick
                else None
            ),
            overdue=overdue,
            today=due_today,
            tomorrow=due_tomorrow,
            done_today=[out(t) for t in done],
            counts=await self.tasks.counts(),
            stories=stories,
            stories_window_hours=window,
            new_stories=new_stories,
            briefing=written,
            goals=[m.text for m in goals],
        )
