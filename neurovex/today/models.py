"""One row per day the morning brief was announced, so it's announced only once."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, utcnow


class DailyBrief(Base):
    __tablename__ = "daily_briefs"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # local date, YYYY-MM-DD
    line: Mapped[str] = mapped_column(String(400))
    announced_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
