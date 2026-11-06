"""API data shapes for the news module."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from neurovex.modules.news.models import InteractionAction, InterestKind


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    source_name: str
    url: str
    title: str
    author: str | None
    published_at: datetime | None
    discovered_at: datetime
    status: str
    excerpt: str | None
    summary: str | None
    what_happened: str | None
    why_it_matters: str | None
    key_points: list[str] | None
    categories: list[str] | None
    topics: list[str] | None = None
    importance: int | None
    summarized_by: str | None
    bookmarked: bool
    read: bool
    hidden: bool = False
    last_error: str | None
    # Personal layer
    score: float | None = None
    reasons: list[str] = Field(default_factory=list)
    followed: bool = False
    is_new: bool = False


class ArticlePage(BaseModel):
    items: list[ArticleOut]
    total: int
    limit: int
    offset: int


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    category: str | None
    enabled: bool
    last_checked_at: datetime | None
    last_error: str | None
    article_count: int = 0


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=8, max_length=1000)
    category: str | None = Field(default=None, max_length=100)


class XSourceCreate(BaseModel):
    handle: str = Field(min_length=1, max_length=200, description="@name or a profile URL")
    category: str | None = Field(default=None, max_length=100)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    provider: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    sources_checked: int
    sources_failed: int
    articles_new: int
    articles_duplicate: int
    articles_summarized: int
    articles_skipped: int
    articles_failed: int
    articles_pruned: int
    duration_s: float | None
    error: str | None


class NewsStatus(BaseModel):
    running: bool
    provider: str
    auto_refresh: bool
    refresh_minutes: int
    next_run_at: datetime | None
    last_run: RunOut | None
    counts: dict[str, int]
    last_seen_at: datetime | None = None
    new_since_seen: int = 0
    briefing_at: datetime | None = None
    briefing_running: bool = False


class CategoryCount(BaseModel):
    name: str
    count: int


class FlagIn(BaseModel):
    value: bool = True


# --- personal layer --------------------------------------------------------------------


class FeedbackIn(BaseModel):
    action: InteractionAction


class InterestIn(BaseModel):
    kind: InterestKind = InterestKind.TOPIC
    value: str = Field(min_length=1, max_length=200)
    mode: Literal["follow", "mute"] = "follow"


class InterestOut(BaseModel):
    id: int
    kind: str
    value: str
    label: str
    mode: Literal["follow", "mute"]


class LearnedOut(BaseModel):
    key: str
    label: str
    kind: str
    affinity: float


class ProfileOut(BaseModel):
    following: list[InterestOut]
    muted: list[InterestOut]
    learned_likes: list[LearnedOut]
    learned_dislikes: list[LearnedOut]
    interactions: int
    trending_topics: list[CategoryCount]


class BriefingItemOut(BaseModel):
    article_id: int
    take: str
    title: str
    url: str
    source_name: str
    importance: int | None


class BriefingOut(BaseModel):
    id: int
    created_at: datetime
    provider: str
    trigger: str
    headline: str
    overview: str
    items: list[BriefingItemOut]
    watch: str | None


class CatalogFeed(BaseModel):
    name: str
    url: str
    added: bool


class CatalogGroup(BaseModel):
    name: str
    feeds: list[CatalogFeed]
