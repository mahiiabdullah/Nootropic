"""Memory table plus its full-text index (SQLite FTS5, kept in sync by triggers)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, ensure_fts, utcnow


class MemoryKind(StrEnum):
    FACT = "fact"  # about the user: "Works as a backend developer"
    PREFERENCE = "preference"  # "Prefers short answers", "Dislikes crypto news"
    INSTRUCTION = "instruction"  # standing orders for Neurovex: "Call me Mahi"
    PERSON = "person"  # "Sister Ayesha lives in Toronto"
    GOAL = "goal"  # "Wants to ship Neurovex v1 by March"
    EVENT = "event"  # "Started learning Rust on 2026-10-14"


KINDS: list[str] = [k.value for k in MemoryKind]


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    text: Mapped[str] = mapped_column(String(400))
    importance: Mapped[int] = mapped_column(Integer, default=2)  # 1 minor · 2 normal · 3 core
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="chat")  # chat | user
    conversation_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    use_count: Mapped[int] = mapped_column(Integer, default=0)


def install(conn) -> None:  # type: ignore[no-untyped-def]
    """Create the FTS5 index over memories (called by init_db)."""
    ensure_fts(conn, table="memories", fts="memories_fts", column="text")
