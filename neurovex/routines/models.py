"""The routines table: a name, what you say to run it, and the steps it runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, utcnow


class Routine(Base):
    __tablename__ = "routines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    triggers: Mapped[list[str]] = mapped_column(JSON)  # normalized phrases: "start my day"
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)  # [{"tool": …, "args": {…}}]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    use_count: Mapped[int] = mapped_column(Integer, default=0)
