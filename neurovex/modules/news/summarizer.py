"""LLM writing for the news module, through the Neurovex LLM router.

Two jobs:
- `summarize(article)`: a structured brief of one article (`ArticleSummary`)
- `write_briefing(request)`: a personal rundown of several stories (`BriefingDraft`)

Requests go to the provider chain in `neurovex.llm` (Cerebras, Groq, Gemini,
Mistral, OpenRouter, Cloudflare, Hugging Face, plus optional Claude/Ollama), with
failover when a provider is rate limited, down, or returns invalid JSON. Web
content is untrusted and fenced in tags; the prompts tell the model to treat it as data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

from neurovex.core.logging import get_logger
from neurovex.llm.errors import ProvidersExhausted
from neurovex.llm.types import JSONRequest

if TYPE_CHECKING:
    import httpx

    from neurovex.core.config import Settings
    from neurovex.llm.router import LLMRouter

log = get_logger(__name__)
PROMPTS = Path(__file__).parent / "prompts"


class Category(StrEnum):
    AI = "AI"
    CYBERSECURITY = "Cybersecurity"
    PROGRAMMING = "Programming"
    HARDWARE = "Hardware"
    CLOUD = "Cloud & DevOps"
    SCIENCE = "Science"
    BUSINESS = "Business"
    POLICY = "Policy"
    TECHNOLOGY = "Technology"


CATEGORIES: list[str] = [c.value for c in Category]


# --- article summary ----------------------------------------------------------------


class ArticleSummary(BaseModel):
    relevant: bool
    title: str
    summary: str
    what_happened: str
    why_it_matters: str
    key_points: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    importance: int
    model: str | None = Field(default=None, exclude=True)  # set by the writer, not the LLM

    @field_validator("importance", mode="before")
    @classmethod
    def _clamp_importance(cls, v: Any) -> int:
        return max(1, min(10, int(v)))

    @field_validator("categories", mode="before")
    @classmethod
    def _known_categories_only(cls, v: Any) -> list[str]:
        # Models occasionally invent a label ("Space", "cybersecurity"): keep the valid
        # ones (case-insensitive) instead of throwing away the whole summary.
        by_lower = {c.lower(): c for c in CATEGORIES}
        items = v if isinstance(v, list) else []
        return [
            by_lower[x.strip().lower()]
            for x in items
            if isinstance(x, str) and x.strip().lower() in by_lower
        ]

    @field_validator("categories")
    @classmethod
    def _dedupe_categories(cls, v: list[Category]) -> list[Category]:
        out = list(dict.fromkeys(v))[:3]
        return out or [Category.TECHNOLOGY]

    @field_validator("key_points")
    @classmethod
    def _trim_points(cls, v: list[str]) -> list[str]:
        return [p.strip() for p in v if p and p.strip()][:6]

    @field_validator("topics")
    @classmethod
    def _clean_topics(cls, v: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for t in v:
            t = (t or "").strip().strip("#").strip()
            if 1 < len(t) <= 40 and t.lower() not in seen:
                seen[t.lower()] = t
        return list(seen.values())[:6]


# Hand-written so it stays within the JSON Schema subset structured outputs support.
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "what_happened": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "integer"},
    },
    "required": [
        "relevant", "title", "summary", "what_happened", "why_it_matters",
        "key_points", "categories", "topics", "importance",
    ],
    "additionalProperties": False,
}  # fmt: skip


@dataclass(frozen=True)
class ArticleInput:
    source_name: str
    title: str
    url: str
    published_at: datetime | None
    text: str


# --- personal briefing ---------------------------------------------------------------


@dataclass(frozen=True)
class BriefingStory:
    article_id: int
    title: str
    source_name: str
    summary: str
    why_it_matters: str
    importance: int
    reasons: list[str]  # why it was picked for this reader


@dataclass(frozen=True)
class BriefingRequest:
    local_time: str
    following: list[str]
    learned_interests: list[str]
    stories: list[BriefingStory]


class BriefingItem(BaseModel):
    article_id: int
    take: str


class BriefingDraft(BaseModel):
    headline: str
    overview: str
    items: list[BriefingItem]
    watch: str
    model: str | None = Field(default=None, exclude=True)  # set by the writer, not the LLM


BRIEFING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "overview": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"article_id": {"type": "integer"}, "take": {"type": "string"}},
                "required": ["article_id", "take"],
                "additionalProperties": False,
            },
        },
        "watch": {"type": "string"},
    },
    "required": ["headline", "overview", "items", "watch"],
    "additionalProperties": False,
}


# --- writing via the LLM router ---------------------------------------------------------


class SummarizeError(Exception):
    """The model could not produce valid output (counts as one failed attempt)."""


class ProvidersUnavailable(SummarizeError):
    """No provider can take requests right now (rate limits, outages, missing keys).

    Not the article's fault: the pipeline leaves it queued without using an attempt.
    """

    def __init__(self, message: str, retry_in_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_in_s = retry_in_s


class Summarizer(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def is_local(self) -> bool: ...

    async def summarize(self, article: ArticleInput) -> ArticleSummary: ...

    async def write_briefing(self, request: BriefingRequest) -> BriefingDraft: ...


# ~1.1k tokens: the opening carries the news. Halved from 8,000 to save input tokens.
MAX_ARTICLE_CHARS = 4_000
# Sized to real output (median ~400, max seen ~670 tokens): providers like Groq charge the
# full max_tokens against their per-minute budget up front.
SUMMARY_MAX_TOKENS = 1_000
BRIEFING_MAX_TOKENS = 3_000
# Background work may wait this long for a provider's rate budget instead of failing.
BACKGROUND_MAX_WAIT_S = 90.0
# Leave this much of each provider's per-minute token budget for chat.
BACKGROUND_RESERVE_TOKENS = 2_500
TRANSIENT_KINDS = {"rate_limited", "quota", "unavailable", "auth", "model"}


@cache
def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def system_prompt() -> str:
    return prompt("summarize")


def build_user_message(article: ArticleInput, max_chars: int) -> str:
    # Long articles are cut to their opening, which carries the news.
    text = article.text[:max_chars]
    published = article.published_at.isoformat() if article.published_at else "unknown"
    return (
        f"Source: {article.source_name}\n"
        f"Title: {article.title}\n"
        f"URL: {article.url}\n"
        f"Published: {published}\n\n"
        f"<article>\n{text}\n</article>"
    )


def build_briefing_message(req: BriefingRequest) -> str:
    learned = ", ".join(req.learned_interests) or "not enough history yet"
    lines = [
        f"Reader's local time: {req.local_time}",
        f"Reader follows: {', '.join(req.following) or 'nothing explicitly yet'}",
        f"Reader tends to read about: {learned}",
        "",
        "<stories>",
    ]
    for s in req.stories:
        lines += [
            f'<story id="{s.article_id}">',
            f"Title: {s.title}",
            f"Source: {s.source_name} | Importance: {s.importance}/10",
            f"Picked because: {'; '.join(s.reasons) or 'generally important'}",
            f"Summary: {s.summary}",
            f"Why it matters: {s.why_it_matters}",
            "</story>",
        ]
    lines.append("</stories>")
    return "\n".join(lines)


def _parse[M: BaseModel](model: type[M], raw: str) -> M:
    try:
        return model.model_validate_json(raw)
    except ValidationError as e:
        raise SummarizeError(f"Invalid JSON from model: {e.errors()[:2]}") from e


def parse_summary(raw: str) -> ArticleSummary:
    return _parse(ArticleSummary, raw)


MIN_BRIEFING_ITEMS = 4


def complete_briefing(draft: BriefingDraft, request: BriefingRequest) -> BriefingDraft:
    """Drop invented ids and duplicates; top up stories a small model skipped.

    Stories arrive ranked for the reader, so any of the top ones the model left
    out are appended with their own "why it matters" line.
    """
    allowed = {s.article_id: s for s in request.stories}
    seen: set[int] = set()
    items: list[BriefingItem] = []
    for item in draft.items:
        if item.article_id in allowed and item.article_id not in seen and item.take.strip():
            seen.add(item.article_id)
            items.append(item)
    if not items:
        raise SummarizeError("Briefing referenced none of the provided stories")
    want = min(MIN_BRIEFING_ITEMS, len(request.stories))
    for story in request.stories:
        if len(items) >= want:
            break
        if story.article_id not in seen and story.why_it_matters:
            items.append(BriefingItem(article_id=story.article_id, take=story.why_it_matters))
            seen.add(story.article_id)
    draft.items = items
    return draft


class RouterSummarizer:
    """Summaries and briefings through the provider chain (or one pinned provider)."""

    def __init__(self, router: LLMRouter, only: str | None = None) -> None:
        self.router = router
        self.only = only

    @property
    def name(self) -> str:
        if self.only:
            st = self.router.states[self.only]
            return f"{self.only}/{st.provider.model}" if st.provider else self.only
        return self.router.label()

    @property
    def is_local(self) -> bool:
        if self.only:
            st = self.router.states[self.only]
            return bool(st.provider and st.provider.is_local)
        return self.router.all_local

    async def summarize(self, article: ArticleInput) -> ArticleSummary:
        req = JSONRequest(
            system=system_prompt(),
            user=build_user_message(article, MAX_ARTICLE_CHARS),
            schema=SUMMARY_SCHEMA,
            schema_name="article_summary",
            max_tokens=SUMMARY_MAX_TOKENS,
            task="news.summarize",
        )
        summary, result = await self._run(req, parse_summary)
        summary.model = result.label
        return summary

    async def write_briefing(self, request: BriefingRequest) -> BriefingDraft:
        req = JSONRequest(
            system=prompt("briefing"),
            user=build_briefing_message(request),
            schema=BRIEFING_SCHEMA,
            schema_name="news_briefing",
            max_tokens=BRIEFING_MAX_TOKENS,
            task="news.briefing",
        )
        draft, result = await self._run(
            req, lambda raw: complete_briefing(_parse(BriefingDraft, raw), request)
        )
        draft.model = result.label
        return draft

    async def _run[T](self, req: JSONRequest, parse: Callable[[str], T]) -> tuple[T, Any]:
        try:
            return await self.router.complete_json(
                req,
                parse,
                only=self.only,
                max_wait_s=BACKGROUND_MAX_WAIT_S,
                reserve_tokens=BACKGROUND_RESERVE_TOKENS,
            )
        except ProvidersExhausted as e:
            # Every failure was about providers (limits, outages, keys), not this input.
            if not e.kinds or set(e.kinds) <= TRANSIENT_KINDS:
                raise ProvidersUnavailable(str(e), e.retry_in_s) from e
            raise SummarizeError(str(e)) from e


SummarizerFactory = Callable[["Settings", "httpx.AsyncClient"], Awaitable[Summarizer | None]]


def router_summarizer_factory(router: LLMRouter) -> SummarizerFactory:
    """News writer factory: re-reads keys each run, honours `news_llm_provider`."""

    async def factory(settings: Settings, _http: httpx.AsyncClient) -> Summarizer | None:
        choice = settings.news_llm_provider
        if choice == "none":
            return None
        router.refresh()
        if choice == "auto":
            return RouterSummarizer(router) if router.has_providers() else None
        if choice not in router.states or not router.states[choice].configured:
            log.warning("news_pinned_provider_unavailable", provider=choice)
            return None
        return RouterSummarizer(router, only=choice)

    return factory
