"""Where a task stands and what to do next: pure functions, no AI, no database.

`suggest()` is the advisor behind "what should I do now?": it ranks open tasks by
how late or close they are, priority, how long they've waited and how often they
were pushed back, and fits them to the time you have. Every score comes with
plain-language reasons, so the answer can be explained (and checked) without a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from neurovex.modules.tasks.models import Task

PRIORITY_NAMES = {1: "low", 2: "normal", 3: "high", 4: "urgent"}
BUCKETS = ("overdue", "today", "upcoming", "later", "someday")
UPCOMING_DAYS = 7


def _days_from_today(when: datetime, now: datetime, tz: tzinfo) -> int:
    return (when.astimezone(tz).date() - now.astimezone(tz).date()).days


def bucket(task: Task, now: datetime, tz: tzinfo) -> str:
    if task.due_at is None:
        return "someday"
    if task.due_at < now:
        return "overdue"
    days = _days_from_today(task.due_at, now, tz)
    if days <= 0:
        return "today"
    return "upcoming" if days <= UPCOMING_DAYS else "later"


def due_label(task: Task, now: datetime, tz: tzinfo) -> str | None:
    """ "today 17:00", "tomorrow", "Fri", "Fri 24 Oct", "yesterday"…"""
    if task.due_at is None:
        return None
    local = task.due_at.astimezone(tz)
    days = _days_from_today(task.due_at, now, tz)
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif days == -1:
        day = "yesterday"
    elif 1 < days < 7:
        day = local.strftime("%a")
    else:
        day = f"{local.strftime('%a')} {local.day} {local.strftime('%b')}"
        if local.year != now.astimezone(tz).year:
            day += f" {local.year}"
    return day if task.all_day else f"{day} {local.strftime('%H:%M')}"


def _late_by(task: Task, now: datetime, tz: tzinfo) -> str:
    days = -_days_from_today(task.due_at, now, tz)  # type: ignore[arg-type]
    if days >= 1:
        return f"overdue by {days} day{'s' if days != 1 else ''}"
    hours = int((now - task.due_at).total_seconds() // 3600)  # type: ignore[operator]
    return f"was due {hours} h ago" if hours else "just became due"


@dataclass
class Suggestion:
    task: Task
    score: float
    reasons: list[str]


def score(
    task: Task, now: datetime, tz: tzinfo, minutes: int | None = None
) -> tuple[float, list[str]]:
    points, reasons = 0.0, []
    where = bucket(task, now, tz)
    if where == "overdue":
        days = max(0, -_days_from_today(task.due_at, now, tz))  # type: ignore[arg-type]
        points += 60 + min(days, 7) * 3
        reasons.append(_late_by(task, now, tz))
    elif where == "today":
        if task.all_day:
            points += 45
            reasons.append("due today")
        else:
            left = (task.due_at - now).total_seconds() / 3600  # type: ignore[operator]
            points += 55 if left <= 2 else 48
            when = task.due_at.astimezone(tz).strftime("%H:%M")  # type: ignore[union-attr]
            soon = f"{max(1, round(left * 60))} min" if left < 1 else f"{left:.0f} h"
            reasons.append(f"due at {when} (in {soon})")
    elif where == "upcoming":
        days = _days_from_today(task.due_at, now, tz)  # type: ignore[arg-type]
        points += 28 if days == 1 else max(8, 22 - 2 * days)
        reasons.append("due tomorrow" if days == 1 else f"due {due_label(task, now, tz)}")
    elif where == "later":
        points += 3

    bonus = {4: 35, 3: 20, 2: 8, 1: 0}.get(task.priority, 8)
    points += bonus
    if task.priority >= 3:
        reasons.append(f"{PRIORITY_NAMES[task.priority]} priority")

    waited = (now - task.created_at).days
    if task.due_at is None and waited >= 7:
        points += min(waited, 30) / 3
        reasons.append(f"waiting {waited} days")
    if task.deferrals >= 2:
        points += min(task.deferrals, 3) * 6
        reasons.append(f"pushed back {task.deferrals}×: do it, split it or drop it")

    if task.estimate_min:
        if minutes is not None and task.estimate_min > minutes:
            points -= 40
            reasons.append(f"needs ~{task.estimate_min} min, more than you have")
        elif minutes is not None:
            points += 10
            reasons.append(f"fits your {minutes} min (~{task.estimate_min})")
        elif task.estimate_min <= 10:
            points += 4
            reasons.append(f"quick win (~{task.estimate_min} min)")
    return points, reasons


def suggest(
    tasks: list[Task], now: datetime, tz: tzinfo, minutes: int | None = None, limit: int = 3
) -> list[Suggestion]:
    ranked = []
    for t in tasks:
        if t.status != "open":
            continue
        points, reasons = score(t, now, tz, minutes)
        ranked.append(Suggestion(t, points, reasons))
    far = now + timedelta(days=36500)
    ranked.sort(key=lambda s: (-s.score, s.task.due_at or far, -s.task.priority, s.task.id))
    return ranked[:limit]


def describe(task: Task, now: datetime, tz: tzinfo) -> dict[str, Any]:
    """Everything the UI shows for one task."""
    return {
        "id": task.id,
        "title": task.title,
        "notes": task.notes,
        "status": task.status,
        "priority": task.priority,
        "priority_name": PRIORITY_NAMES.get(task.priority, "normal"),
        "due_at": task.due_at,
        "all_day": task.all_day,
        "due_label": due_label(task, now, tz),
        "bucket": bucket(task, now, tz) if task.status == "open" else task.status,
        "remind_at": task.remind_at,
        "repeat": task.repeat,
        "tags": task.tags or [],
        "estimate_min": task.estimate_min,
        "deferrals": task.deferrals,
        "source": task.source,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
    }


def brief(task: Task, now: datetime, tz: tzinfo) -> dict[str, Any]:
    """What the model sees per task: only fields that carry information."""
    out: dict[str, Any] = {"id": task.id, "title": task.title}
    if task.status != "open":
        out["status"] = task.status
    if task.due_at is not None:
        out["due"] = due_label(task, now, tz)
        if task.status == "open" and task.due_at < now:
            out["overdue"] = True
    if task.priority != 2:
        out["priority"] = PRIORITY_NAMES.get(task.priority)
    for key in ("repeat", "estimate_min", "notes"):
        if value := getattr(task, key):
            out[key] = value if key != "notes" else value[:160]
    if task.tags:
        out["tags"] = task.tags
    return out
