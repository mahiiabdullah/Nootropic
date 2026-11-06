"""Today in conversation: "brief me", "how's my day?"."""

from __future__ import annotations

from typing import Any

from neurovex.core.skills import NoParams, Skill, SkillResult
from neurovex.today.brief import TodayBrief

TODAY_HINTS = (
    r"\b(my day|today|brief me|catch me up|start my day|good morning|morning brief|overview"
    r"|what's up|whats up)\b"
)


def today_skills(brief: TodayBrief) -> list[Skill]:
    async def overview(_: NoParams) -> SkillResult:
        t = await brief.build()

        def task(x: Any) -> dict[str, Any]:
            return {"id": x.id, "title": x.title, "due": x.due_label, "priority": x.priority_name}

        data = {
            "date": t.date_label,
            "summary": t.line,
            "do_first": (
                {"title": t.next.task.title, "why": "; ".join(t.next.reasons) or "next in line"}
                if t.next
                else None
            ),
            "overdue": [task(x) for x in t.overdue[:8]],
            "due_today": [task(x) for x in t.today[:8]],
            "due_tomorrow": [task(x) for x in t.tomorrow[:5]],
            "done_today": [x.title for x in t.done_today[:8]],
            "top_stories": [
                {"title": a.title, "source": a.source_name, "summary": (a.summary or "")[:160]}
                for a in t.stories
            ],
            "news_briefing": (
                {"headline": t.briefing.headline, "overview": t.briefing.overview}
                if t.briefing
                else None
            ),
            "goals": t.goals,
        }
        items = [*t.overdue, *t.today][:8]
        display = {
            "kind": "tasks",
            "items": [
                {
                    "id": x.id,
                    "title": x.title,
                    "status": x.status,
                    "due_label": x.due_label,
                    "overdue": x.bucket == "overdue",
                    "priority": x.priority,
                }  # fmt: skip
                for x in items
            ],
        }
        return SkillResult(
            ok=True, summary=f"Today: {t.line}"[:200], data=data, display=display if items else None
        )

    return [
        Skill(
            "today.brief",
            "The user's day at a glance: tasks due, what to do first, top stories, goals.",
            overview,
        )
    ]
