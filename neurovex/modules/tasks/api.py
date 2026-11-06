"""Tasks REST API: /api/tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from neurovex.core.db import utcnow
from neurovex.core.live import sse_stream
from neurovex.modules.tasks.dates import parse_quick
from neurovex.modules.tasks.models import Task
from neurovex.modules.tasks.module import TasksModule
from neurovex.modules.tasks.planner import BUCKETS, describe, suggest
from neurovex.modules.tasks.store import VIEWS, TaskError, TaskStore

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _module(request: Request) -> TasksModule:
    return request.app.state.tasks


def _store(request: Request) -> TaskStore:
    return _module(request).store


class TaskOut(BaseModel):
    id: int
    title: str
    notes: str | None
    status: str
    priority: int
    priority_name: str
    due_at: datetime | None
    all_day: bool
    due_label: str | None
    bucket: str
    remind_at: datetime | None
    repeat: str | None
    tags: list[str]
    estimate_min: int | None
    deferrals: int
    source: str
    created_at: datetime
    completed_at: datetime | None


class TaskIn(BaseModel):
    """Either `text` (quick-add: "Call bank tomorrow 10am !high #errands") or `title` + fields.
    Explicit fields win over what's read from `text`."""

    text: str | None = Field(default=None, max_length=400)
    title: str | None = Field(default=None, max_length=200)
    due: str | None = Field(default=None, max_length=60, description="'fri 3pm', '2026-10-20'")
    priority: int | None = Field(default=None, ge=1, le=4)
    notes: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    repeat: str | None = None
    estimate_min: int | None = Field(default=None, ge=1, le=1440)


class TaskPatch(BaseModel):
    """Only the fields you send change. `due: null` (or "") clears the due date.
    `text` is quick-add syntax: its title replaces the title, and any date, !priority,
    #tags, ~estimate or repeat found in it replace those too."""

    text: str | None = Field(default=None, max_length=400)
    title: str | None = Field(default=None, max_length=200)
    due: str | None = Field(default=None, max_length=60)
    priority: int | None = Field(default=None, ge=1, le=4)
    notes: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    repeat: str | None = None
    estimate_min: int | None = Field(default=None, ge=0, le=1440)


class SnoozeIn(BaseModel):
    minutes: int = Field(default=10, ge=1, le=24 * 60)


class PostponeIn(BaseModel):
    days: int = Field(default=1, ge=1, le=365)


class CompleteOut(BaseModel):
    task: TaskOut
    next: TaskOut | None


class SuggestionOut(BaseModel):
    task: TaskOut
    score: float
    reasons: list[str]


class AgendaOut(BaseModel):
    groups: dict[str, list[TaskOut]]
    done_today: list[TaskOut]
    counts: dict[str, int]


def _out(store: TaskStore, task: Task) -> TaskOut:
    return TaskOut(**describe(task, utcnow(), store.tz))


def _bad(e: TaskError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


async def _existing(store: TaskStore, task_id: int) -> Task:
    task = await store.get(task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return task


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    request: Request,
    view: Annotated[str, Query(description=", ".join(VIEWS))] = "open",
    q: Annotated[str, Query(max_length=100)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[TaskOut]:
    store = _store(request)
    try:
        return [_out(store, t) for t in await store.list(view, q, limit)]
    except TaskError as e:
        raise _bad(e) from e


@router.get("/agenda", response_model=AgendaOut)
async def agenda(request: Request) -> AgendaOut:
    """Open tasks grouped overdue / today / upcoming / later / someday, plus what you did today."""
    store = _store(request)
    groups: dict[str, list[TaskOut]] = {b: [] for b in BUCKETS}
    for t in await store.list("open", limit=500):
        out = _out(store, t)
        groups[out.bucket].append(out)
    done = [_out(store, t) for t in await store.done_today()]
    return AgendaOut(groups=groups, done_today=done, counts=await store.counts())


@router.get("/counts")
async def counts(request: Request) -> dict[str, int]:
    """Open tasks: overdue, due later today, and all (for badges)."""
    return await _store(request).counts()


@router.get("/next", response_model=list[SuggestionOut])
async def next_up(
    request: Request,
    minutes: Annotated[int | None, Query(ge=5, le=600)] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 3,
) -> list[SuggestionOut]:
    """What to do now, ranked with reasons (no AI involved)."""
    store = _store(request)
    ranked = suggest(await store.list("open", limit=500), utcnow(), store.tz, minutes, limit)
    return [SuggestionOut(task=_out(store, s.task), score=s.score, reasons=s.reasons)
            for s in ranked]  # fmt: skip


@router.get("/parse")
async def parse(request: Request, text: Annotated[str, Query(max_length=400)]) -> dict[str, Any]:
    """Preview what quick-add would make of `text`, without saving anything."""
    store = _store(request)
    p = parse_quick(text, store.local_now())
    preview = Task(
        id=0, title=p.title, status="open", priority=p.priority or 2,
        due_at=p.due.at if p.due else None, all_day=p.due.all_day if p.due else True,
        repeat=p.repeat, tags=p.tags, estimate_min=p.estimate_min, deferrals=0, source="user",
        created_at=utcnow(),
    )  # fmt: skip
    return describe(preview, utcnow(), store.tz)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Live task changes and reminders (Server-Sent Events)."""
    return StreamingResponse(
        sse_stream(_module(request).live, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, request: Request) -> TaskOut:
    store = _store(request)
    return _out(store, await _existing(store, task_id))


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def add(body: TaskIn, request: Request) -> TaskOut:
    store = _store(request)
    try:
        parsed = parse_quick(body.text, store.local_now()) if body.text else None
        title = body.title or (parsed.title if parsed else "")
        due = store.parse_due(body.due) if body.due else (parsed.due if parsed else None)
        task = await store.add(
            title,
            due=due,
            priority=body.priority or (parsed.priority if parsed else None) or 2,
            notes=body.notes,
            tags=body.tags if body.tags is not None else (parsed.tags if parsed else []),
            repeat=body.repeat or (parsed.repeat if parsed else None),
            estimate_min=body.estimate_min or (parsed.estimate_min if parsed else None),
        )
    except TaskError as e:
        raise _bad(e) from e
    return _out(store, task)


@router.patch("/{task_id}", response_model=TaskOut)
async def edit(task_id: int, body: TaskPatch, request: Request) -> TaskOut:
    store = _store(request)
    await _existing(store, task_id)
    changes: dict[str, Any] = {}
    sent = body.model_fields_set
    try:
        if body.text:
            p = parse_quick(body.text, store.local_now())
            found = {"title": p.title, "due": p.due, "priority": p.priority, "tags": p.tags,
                     "repeat": p.repeat, "estimate_min": p.estimate_min}  # fmt: skip
            changes = {k: v for k, v in found.items() if v}
        for name in ("title", "notes", "priority", "tags", "estimate_min"):
            if name in sent:
                changes[name] = getattr(body, name)
        if "due" in sent:
            changes["due"] = store.parse_due(body.due)  # None/"" clears it
        if "repeat" in sent:
            changes["repeat"] = body.repeat or None
        if changes.get("priority", 2) is None or changes.get("title", "x") is None:
            raise TaskError("title and priority can't be empty")
        task = await store.update(task_id, **changes)
    except TaskError as e:
        raise _bad(e) from e
    return _out(store, task)


@router.post("/{task_id}/complete", response_model=CompleteOut)
async def complete(task_id: int, request: Request) -> CompleteOut:
    store = _store(request)
    await _existing(store, task_id)
    task, nxt = await store.complete(task_id)
    return CompleteOut(task=_out(store, task), next=_out(store, nxt) if nxt else None)


@router.post("/{task_id}/reopen", response_model=TaskOut)
async def reopen(task_id: int, request: Request) -> TaskOut:
    store = _store(request)
    await _existing(store, task_id)
    return _out(store, await store.reopen(task_id))


@router.post("/{task_id}/snooze", response_model=TaskOut)
async def snooze(task_id: int, body: SnoozeIn, request: Request) -> TaskOut:
    store = _store(request)
    await _existing(store, task_id)
    return _out(store, await store.snooze(task_id, body.minutes))


@router.post("/{task_id}/postpone", response_model=TaskOut)
async def postpone(task_id: int, body: PostponeIn, request: Request) -> TaskOut:
    store = _store(request)
    await _existing(store, task_id)
    return _out(store, await store.postpone(task_id, body.days))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(task_id: int, request: Request) -> None:
    if not await _store(request).delete(task_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
