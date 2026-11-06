"""Conversation history (tables prefixed `chat_`)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, utcnow


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    steps: Mapped[list[dict] | None] = mapped_column(JSON)  # tools used for this answer
    cards: Mapped[list[dict] | None] = mapped_column(JSON)  # UI cards (e.g. article lists)
    model: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[bool | None] = mapped_column(default=False)
    memory_ops: Mapped[list[dict] | None] = mapped_column(JSON)  # what this answer remembered
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def install(conn) -> None:  # type: ignore[no-untyped-def]
    """Full-text index over past messages, so old conversations can be recalled for free."""
    from neurovex.core.db import ensure_fts

    ensure_fts(conn, table="chat_messages", fts="chat_messages_fts", column="content")
