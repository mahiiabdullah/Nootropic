"""What the news module lets Neurovex do in conversation (registered as skills)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete

from neurovex.core.skills import NoParams, Risk, Skill, SkillResult
from neurovex.modules.news import store
from neurovex.modules.news.briefing import briefing_out
from neurovex.modules.news.models import Interest, InterestKind
from neurovex.modules.news.personalize import norm
from neurovex.modules.news.schemas import ArticleOut
from neurovex.modules.news.summarizer import CATEGORIES

if TYPE_CHECKING:
    from neurovex.modules.news.module import NewsModule

MAX_SUMMARY_CHARS = 200  # every tool result is resent on later steps: keep it lean
# Words that suggest a chat request is about news (the prompt then shows these tools in full).
NEWS_HINTS = (
    r"\b(news|stor(y|ies)|articles?|headlines?|brief\w*|follow\w*|unfollow|mute\w*|topics?"
    r"|trending|feeds?|sources?|happening|announc\w*|releas\w*|launch\w*|security|cyber\w*"
    r"|vulnerab\w*|breach\w*|hack\w*|ai|llms?|openai|google|microsoft|apple|linux|windows"
    r"|rust|python|chips?|hardware|tech\w*|important|latest|updates?|refresh|read)\b"
)


def _brief(a: ArticleOut) -> dict[str, Any]:
    """What the model sees per article: enough to answer, small enough to stay cheap."""
    return {
        "id": a.id,
        "title": a.title,
        "source": a.source_name,
        "published": (a.published_at or a.discovered_at).isoformat(timespec="minutes"),
        "importance": a.importance,
        "summary": (a.summary or a.excerpt or "")[:MAX_SUMMARY_CHARS] or None,
        "why_picked": a.reasons or None,
    }


def _card(items: list[ArticleOut]) -> dict[str, Any]:
    return {
        "kind": "articles",
        "items": [
            {
                "id": a.id,
                "title": a.title,
                "url": a.url,
                "source": a.source_name,
                "importance": a.importance,
                "published": (a.published_at or a.discovered_at).isoformat(),
            }  # fmt: skip
            for a in items
        ],
    }


class SearchParams(BaseModel):
    query: str = Field(default="", description="Words to look for; empty = everything")
    category: str | None = Field(default=None, description=f"One of {', '.join(CATEGORIES)}")
    hours: int = Field(default=168, ge=1, le=24 * 30, description="How far back")
    limit: int = Field(default=6, ge=1, le=15)


class TopParams(BaseModel):
    hours: int = Field(default=24, ge=1, le=24 * 7)
    limit: int = Field(default=6, ge=1, le=15)


class ArticleParams(BaseModel):
    id: int = Field(description="Article id from a previous result")


class InterestParams(BaseModel):
    value: str = Field(min_length=1, max_length=60, description="Topic like 'Rust' or a category")
    kind: Literal["topic", "category"] = "topic"


class ValueParams(BaseModel):
    value: str = Field(min_length=1, max_length=60)


class TrendingParams(BaseModel):
    hours: int = Field(default=48, ge=6, le=24 * 7)


def news_skills(news: NewsModule) -> list[Skill]:
    sessions = news.sessions

    async def search(p: SearchParams) -> SkillResult:
        category = next((c for c in CATEGORIES if c.lower() == (p.category or "").lower()), None)
        async with sessions() as s:
            items, total = await store.list_articles(
                s, await store.load_profile(s), sort="for_you", limit=p.limit,
                q=p.query.strip() or None, category=category, since_hours=p.hours,
            )  # fmt: skip
        what = f"'{p.query}'" if p.query.strip() else "recent news"
        if not items:
            return SkillResult(ok=True, summary=f"No stories found for {what}", data=[])
        return SkillResult(
            ok=True,
            summary=f"Found {total} stories for {what}; showing {len(items)}",
            data=[_brief(a) for a in items],
            display=_card(items),
        )

    async def top(p: TopParams) -> SkillResult:
        async with sessions() as s:
            items = await store.digest(s, await store.load_profile(s), hours=p.hours, limit=p.limit)
        if not items:
            return SkillResult(ok=True, summary="Nothing summarized in that window yet", data=[])
        return SkillResult(
            ok=True,
            summary=f"Top {len(items)} stories for you from the last {p.hours}h",
            data=[_brief(a) for a in items],
            display=_card(items),
        )

    async def article(p: ArticleParams) -> SkillResult:
        async with sessions() as s:
            a = await store.get_article(s, p.id)
            if a is None:
                return SkillResult.fail(f"No article with id {p.id}")
            out = store.to_article_out(a)
        return SkillResult(
            ok=True,
            summary=f"Opened #{out.id}: {out.title}",
            data={
                **_brief(out),
                "summary": out.summary or out.excerpt,
                "what_happened": out.what_happened,
                "why_it_matters": out.why_it_matters,
                "key_points": out.key_points,
                "topics": out.topics,
                "url": out.url,
            },
            display=_card([out]),
        )

    async def briefing(_: NoParams) -> SkillResult:
        async with sessions() as s:
            latest = await store.latest_briefing(s)
            if latest is None:
                return SkillResult(
                    ok=True, summary="No briefing written yet", data={"briefing": None}
                )
            b = await briefing_out(s, latest)
        return SkillResult(
            ok=True,
            summary=f"Latest briefing: {b.headline}",
            data={
                "written_at": b.created_at.isoformat(timespec="minutes"),
                "headline": b.headline,
                "overview": b.overview,
                "items": [{"id": i.article_id, "title": i.title, "take": i.take} for i in b.items],
                "watch": b.watch,
            },
        )

    async def write_briefing(_: NoParams) -> SkillResult:
        started = news.trigger_briefing()
        return SkillResult(
            ok=True,
            summary="Started writing a new briefing"
            if started
            else "A briefing is already being written",
            data={"started": started, "note": "It appears on the For you tab in a minute or two."},
        )

    async def set_interest(p: InterestParams, weight: float) -> SkillResult:
        kind = InterestKind(p.kind)
        value = p.value.strip()
        if kind is InterestKind.CATEGORY:
            match = next((c for c in CATEGORIES if c.lower() == value.lower()), None)
            if match is None:
                return SkillResult.fail(
                    f"'{value}' isn't a category. Categories: {', '.join(CATEGORIES)}"
                )
            value = match
        async with sessions() as s:
            await store.set_interest(s, kind, value, weight)
            await s.commit()
        verb = "Following" if weight > 0 else "Muted"
        return SkillResult(ok=True, summary=f"{verb} {kind.value} '{value}'", data={"done": True})

    async def follow(p: InterestParams) -> SkillResult:
        return await set_interest(p, 1.0)

    async def mute(p: InterestParams) -> SkillResult:
        return await set_interest(p, -1.0)

    async def unfollow(p: ValueParams) -> SkillResult:
        async with sessions() as s:
            result = await s.execute(
                delete(Interest).where(
                    (Interest.value_key == norm(p.value)) | (Interest.value_key == p.value.strip())
                )
            )
            await s.commit()
        if not result.rowcount:
            return SkillResult(
                ok=True, summary=f"'{p.value}' wasn't followed or muted", data={"done": False}
            )
        return SkillResult(
            ok=True, summary=f"Stopped following/muting '{p.value}'", data={"done": True}
        )

    async def interests(_: NoParams) -> SkillResult:
        async with sessions() as s:
            profile = await store.load_profile(s)
        data = {
            "following": [f"{r.value} ({r.kind})" for r in profile.follows],
            "muted": [f"{r.value} ({r.kind})" for r in profile.mutes],
            "learned_likes": [label for _, label, _ in profile.top_learned(8)],
            "learned_dislikes": [label for _, label, _ in profile.top_learned(5, positive=False)],
        }
        return SkillResult(ok=True, summary="Read your interests", data=data)

    async def refresh(_: NoParams) -> SkillResult:
        started = news.trigger_refresh()
        return SkillResult(
            ok=True,
            summary="Checking all feeds now" if started else "A refresh is already running",
            data={"started": started},
        )

    async def trending(p: TrendingParams) -> SkillResult:
        async with sessions() as s:
            topics = await store.trending_topics(s, p.hours)
        return SkillResult(
            ok=True,
            summary=f"{len(topics)} trending topics in the last {p.hours}h",
            data=[{"topic": t, "stories": n} for t, n in topics],
        )

    return [
        Skill(
            "news.search", "Search stored news stories, ranked for the user.", search, SearchParams
        ),
        Skill("news.top", "The most important stories for the user right now.", top, TopParams),
        Skill("news.article", "Full brief of one story by id.", article, ArticleParams),
        Skill("news.briefing", "The user's latest personal news briefing.", briefing),
        Skill(
            "news.write_briefing",
            "Write a fresh personal briefing (slow, uses AI): only if the user asks for a new one.",
            write_briefing,
            risk=Risk.LOCAL_WRITE,
        ),  # fmt: skip
        Skill(
            "news.follow",
            "Follow a topic or category so related stories rank higher.",
            follow,
            InterestParams,
            Risk.LOCAL_WRITE,
        ),  # fmt: skip
        Skill(
            "news.mute",
            "Mute a topic or category so related stories are hidden.",
            mute,
            InterestParams,
            Risk.LOCAL_WRITE,
        ),  # fmt: skip
        Skill("news.unfollow", "Remove a follow or mute.", unfollow, ValueParams, Risk.LOCAL_WRITE),
        Skill("news.interests", "What the user follows, mutes, and tends to read.", interests),
        Skill(
            "news.refresh",
            "Fetch new stories from all feeds now (uses AI): only if the user explicitly asks.",
            refresh,
            risk=Risk.LOCAL_WRITE,
        ),  # fmt: skip
        Skill(
            "news.trending", "Topics appearing in many recent stories.", trending, TrendingParams
        ),
    ]
