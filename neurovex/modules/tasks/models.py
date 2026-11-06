"""Tasks: things you need to do, with due dates, reminders and repeats."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, utcnow


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default=TaskStatus.OPEN, index=True)
    # 1 low · 2 normal · 3 high · 4 urgent
    priority: Mapped[int] = mapped_column(Integer, default=2)
    # All-day tasks are due at the end of their local day (see dates.py).
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    repeat: Mapped[str | None] = mapped_column(String(12))  # daily | weekdays | weekly | monthly
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    estimate_min: Mapped[int | None] = mapped_column(Integer)
    deferrals: Mapped[int] = mapped_column(Integer, default=0)  # times the due date was pushed
    source: Mapped[str] = mapped_column(String(12), default="user")  # user | chat
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
