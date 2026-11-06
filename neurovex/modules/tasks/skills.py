"""What the tasks module lets Neurovex do in conversation (registered as skills).

Tasks are found by id or by words from their title, so "mark 'call bank' done"
takes one tool call instead of a search followed by an update.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from neurovex.core.db import utcnow
from neurovex.core.skills import Risk, Skill, SkillResult
from neurovex.modules.tasks.dates import PRIORITIES
from neurovex.modules.tasks.models import Task
from neurovex.modules.tasks.planner import brief, due_label, suggest
from neurovex.modules.tasks.store import TaskError, TaskStore

# Words that suggest a chat request is about tasks (the prompt then shows these tools in full).
TASK_HINTS = (
    r"\b(tasks?|to-?dos?|remind\w*|deadlines?|due|overdue|agenda|plan\w*|schedul\w*|resched\w*"
    r"|postpone|push|move|done|finish\w*|complete\w*|cancel|drop|should i do|do now|next up"
    r"|need to|have to|must|list|every|today|tonight|tomorrow|tmrw|week|weekend|month"
    r"|mon|tue|wed|thu|fri|sat|sun|(mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}\s?[ap]m"
    r"|minutes?|hours?)\b"
)

Priority = Literal["low", "normal", "high", "urgent"]
Repeat = Literal["", "none", "daily", "weekdays", "weekly", "monthly"]
DUE_HELP = "'tomorrow 5pm', 'fri', 'in 2 hours', 'end of month', '2026-10-20 09:00'"


class AddParams(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="Short, no date in it")
    due: str = Field(default="", description=f"{DUE_HELP}; a time adds a reminder")
    priority: Priority = "normal"
    repeat: Repeat = ""
    estimate_min: int | None = Field(default=None, ge=1, le=1440)
    notes: str = Field(default="", max_length=1000)


class ListParams(BaseModel):
    view: Literal["today", "upcoming", "open", "overdue", "someday", "done"] = Field(
        default="today", description="today includes overdue"
    )
    query: str = Field(default="", max_length=100, description="Title words")


class RefParams(BaseModel):
    task: str = Field(min_length=1, max_length=200, description="Id or title words")


class UpdateParams(RefParams):
    title: str | None = None
    due: str | None = Field(default=None, description="As in tasks.add; 'none' clears")
    priority: Priority | None = None
    repeat: Literal["none", "daily", "weekdays", "weekly", "monthly"] | None = None
    estimate_min: int | None = Field(default=None, ge=1, le=1440)
    notes: str | None = Field(default=None, max_length=1000)


class NextParams(BaseModel):
    minutes: int | None = Field(default=None, ge=5, le=600, description="Time the user has")


def tasks_skills(store: TaskStore) -> list[Skill]:
    def card(tasks: list[Task]) -> dict[str, Any]:
        now = utcnow()
        return {
            "kind": "tasks",
            "items": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "due_label": due_label(t, now, store.tz),
                    "overdue": t.status == "open" and t.due_at is not None and t.due_at < now,
                    "priority": t.priority,
                }  # fmt: skip
                for t in tasks
            ],
        }

    def done(summary: str, tasks: list[Task], data: Any = None) -> SkillResult:
        now = utcnow()
        payload = data if data is not None else [brief(t, now, store.tz) for t in tasks]
        return SkillResult(ok=True, summary=summary, data=payload, display=card(tasks))

    def when(t: Task) -> str:
        label = due_label(t, utcnow(), store.tz)
        return f" · due {label}" if label else ""

    async def add(p: AddParams) -> SkillResult:
        try:
            task = await store.add(
                p.title,
                due=store.parse_due(p.due),
                priority=PRIORITIES[p.priority],
                repeat=None if p.repeat in ("", "none") else p.repeat,
                estimate_min=p.estimate_min,
                notes=p.notes,
                source="chat",
            )
        except TaskError as e:
            return SkillResult.fail(str(e))
        return done(f"Added '{task.title}'{when(task)}", [task])

    async def list_tasks(p: ListParams) -> SkillResult:
        try:
            tasks = await store.list(p.view, p.query, limit=25)
        except TaskError as e:
            return SkillResult.fail(str(e))
        counts = await store.counts()
        return done(
            f"{len(tasks)} task{'s' if len(tasks) != 1 else ''} ({p.view})",
            tasks,
            {"tasks": [brief(t, utcnow(), store.tz) for t in tasks], "totals": counts},
        )

    async def next_up(p: NextParams) -> SkillResult:
        ranked = suggest(await store.list("open", limit=500), utcnow(), store.tz, p.minutes)
        if not ranked:
            return SkillResult(ok=True, summary="No open tasks", data={"suggestions": []})
        data = {
            "suggestions": [
                {**brief(s.task, utcnow(), store.tz), "why": "; ".join(s.reasons) or "open"}
                for s in ranked
            ]
        }
        return done(f"Top pick: {ranked[0].task.title}", [s.task for s in ranked], data)

    async def complete(p: RefParams) -> SkillResult:
        try:
            task, nxt = await store.complete((await store.resolve(p.task)).id)
        except TaskError as e:
            return SkillResult.fail(str(e))
        extra = f"; next one{when(nxt)}" if nxt else ""
        return done(f"Done: '{task.title}'{extra}", [t for t in (task, nxt) if t])

    async def change(p: UpdateParams) -> SkillResult:
        try:
            task = await store.resolve(p.task)
            changes: dict[str, Any] = {}
            if p.title is not None:
                changes["title"] = p.title
            if p.due is not None:
                none = p.due.strip().lower() in ("", "none", "no date", "clear")
                changes["due"] = None if none else store.parse_due(p.due)
            if p.priority is not None:
                changes["priority"] = PRIORITIES[p.priority]
            if p.repeat is not None:
                changes["repeat"] = None if p.repeat == "none" else p.repeat
            if p.estimate_min is not None:
                changes["estimate_min"] = p.estimate_min
            if p.notes is not None:
                changes["notes"] = p.notes
            if not changes:
                return SkillResult.fail("Nothing to change: pass a new title, due, priority…")
            task = await store.update(task.id, **changes)
        except TaskError as e:
            return SkillResult.fail(str(e))
        return done(f"Updated '{task.title}'{when(task)}", [task])

    async def cancel(p: RefParams) -> SkillResult:
        try:
            task = await store.cancel((await store.resolve(p.task)).id)
        except TaskError as e:
            return SkillResult.fail(str(e))
        return done(f"Dropped '{task.title}'", [task])

    return [
        Skill("tasks.add", "Add a to-do. Things the user must do go here, not in memory.",
              add, AddParams, Risk.LOCAL_WRITE),
        Skill("tasks.list", "The user's tasks with ids and due dates.", list_tasks, ListParams),
        Skill("tasks.next", "What to do now: open tasks ranked best first, with reasons.", next_up,
              NextParams),
        Skill("tasks.complete", "Mark a task done (repeats roll to the next date).", complete,
              RefParams, Risk.LOCAL_WRITE),
        Skill("tasks.update", "Rename, reschedule or reprioritize a task.", change,
              UpdateParams, Risk.LOCAL_WRITE),
        Skill("tasks.cancel", "Drop a task the user no longer plans to do.", cancel, RefParams,
              Risk.LOCAL_WRITE),
    ]  # fmt: skip
