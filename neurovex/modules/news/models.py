"""ORM models for the news module (tables prefixed `news_`)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from neurovex.core.db import Base, UTCDateTime, utcnow


class ArticleStatus(StrEnum):
    DISCOVERED = "DISCOVERED"  # stored, waiting for a summary
    PROCESSED = "PROCESSED"  # summarized and relevant
    SKIPPED = "SKIPPED"  # summarized, judged not relevant (hidden by default)
    DUPLICATE = "DUPLICATE"  # same story already stored from another source
    FAILED = "FAILED"  # summary failed; retried until max attempts


class Source(Base):
    __tablename__ = "news_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    category: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(200))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # X accounts only (see xsource.py)
    x_since_id: Mapped[str | None] = mapped_column(String(30))
    x_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    articles: Mapped[list[Article]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class Article(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2000))
    canonical_url: Mapped[str] = mapped_column(String(2000), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    title_hash: Mapped[str] = mapped_column(String(64), index=True)
    author: Mapped[str | None] = mapped_column(String(300))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    excerpt: Mapped[str | None] = mapped_column(Text)  # plain text from the feed

    status: Mapped[str] = mapped_column(
        String(20), default=ArticleStatus.DISCOVERED.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer)

    # Filled by the summarizer
    summary: Mapped[str | None] = mapped_column(Text)
    what_happened: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    key_points: Mapped[list[str] | None] = mapped_column(JSON)
    categories: Mapped[list[str] | None] = mapped_column(JSON)
    topics: Mapped[list[str] | None] = mapped_column(JSON)  # specific: "Rust", "NetScaler"
    importance: Mapped[int | None] = mapped_column(Integer, index=True)
    summarized_by: Mapped[str | None] = mapped_column(String(100))
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # User state
    bookmarked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    hidden_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    source: Mapped[Source] = relationship(back_populates="articles")


class Run(Base):
    __tablename__ = "news_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))  # schedule | manual | cli
    provider: Mapped[str] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")  # RUNNING | OK | ERROR
    sources_checked: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    articles_new: Mapped[int] = mapped_column(Integer, default=0)
    articles_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    articles_summarized: Mapped[int] = mapped_column(Integer, default=0)
    articles_skipped: Mapped[int] = mapped_column(Integer, default=0)
    articles_failed: Mapped[int] = mapped_column(Integer, default=0)
    articles_pruned: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)


# --- personal layer ---------------------------------------------------------------


class InterestKind(StrEnum):
    TOPIC = "topic"  # free text, matched against topics, title and summary
    CATEGORY = "category"
    SOURCE = "source"  # value = source id as text


class Interest(Base):
    """Something you explicitly follow (+1) or mute (-1)."""

    __tablename__ = "news_interests"
    __table_args__ = (UniqueConstraint("kind", "value_key", name="uq_news_interest"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(200))  # as typed, for display
    value_key: Mapped[str] = mapped_column(String(200))  # normalized for matching
    weight: Mapped[float] = mapped_column(Float)  # +1 follow, -1 mute
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class InteractionAction(StrEnum):
    OPEN = "open"
    SAVE = "save"
    MORE = "more"  # "more like this"
    LESS = "less"  # "less like this"
    HIDE = "hide"


class Interaction(Base):
    """Implicit and explicit feedback the personal ranking learns from."""

    __tablename__ = "news_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_articles.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(20))
    # Snapshot of what the article was about, so learning survives article pruning.
    terms: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class Briefing(Base):
    """A personal, LLM-written rundown of the stories that matter to you right now."""

    __tablename__ = "news_briefings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(100))
    trigger: Mapped[str] = mapped_column(String(20))  # auto | manual
    headline: Mapped[str] = mapped_column(String(300))
    overview: Mapped[str] = mapped_column(Text)
    items: Mapped[list[dict]] = mapped_column(JSON)  # [{article_id, take}]
    watch: Mapped[str | None] = mapped_column(Text)


class State(Base):
    """Small key/value store (e.g. when you last looked at the feed)."""

    __tablename__ = "news_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
