"""Personal briefings: an LLM-written rundown of the stories that matter to you now."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.core.logging import get_logger
from neurovex.modules.news import store
from neurovex.modules.news.models import Article, ArticleStatus, Briefing
from neurovex.modules.news.schemas import BriefingItemOut, BriefingOut
from neurovex.modules.news.summarizer import BriefingRequest, BriefingStory, Summarizer

log = get_logger(__name__)

MIN_STORIES = 3
WINDOW_HOURS = 24


class NotEnoughStories(Exception):
    pass


async def should_auto_brief(session: AsyncSession, settings: Settings) -> bool:
    """True when the last briefing is stale and enough new stories have landed since."""
    latest = await store.latest_briefing(session)
    if latest and utcnow() - latest.created_at < timedelta(
        hours=settings.news_briefing_stale_hours
    ):
        return False
    since = latest.created_at if latest else utcnow() - timedelta(hours=WINDOW_HOURS)
    fresh = await session.scalar(
        select(func.count())
        .select_from(Article)
        .where(Article.status == ArticleStatus.PROCESSED.value)
        .where(Article.processed_at > since)
    )
    return (fresh or 0) >= MIN_STORIES


def _local_time_label(now: datetime) -> str:
    local = now.astimezone()
    return local.strftime("%A %d %B %Y, %H:%M")


async def generate_briefing(
    sessions: async_sessionmaker[AsyncSession],
    writer: Summarizer,
    settings: Settings,
    trigger: str = "manual",
) -> Briefing:
    async with sessions() as s:
        profile = await store.load_profile(s)
        picked = await store.top_for_you(
            s, profile, hours=WINDOW_HOURS, limit=settings.news_briefing_size
        )
    if len(picked) < MIN_STORIES:
        raise NotEnoughStories(
            f"Need at least {MIN_STORIES} summarized stories from the last {WINDOW_HOURS}h"
        )

    request = BriefingRequest(
        local_time=_local_time_label(utcnow()),
        following=[r.value for r in profile.follows],
        learned_interests=[label for _, label, _ in profile.top_learned(6)],
        stories=[
            BriefingStory(
                article_id=a.id,
                title=a.title,
                source_name=a.source.name,
                summary=a.summary or "",
                why_it_matters=a.why_it_matters or "",
                importance=a.importance or 0,
                reasons=ev.reasons,
            )
            for a, ev in picked
        ],
    )
    draft = await writer.write_briefing(request)
    async with sessions() as s:
        briefing = Briefing(
            provider=draft.model or writer.name,
            trigger=trigger,
            headline=draft.headline.strip()[:300],
            overview=draft.overview.strip(),
            items=[i.model_dump() for i in draft.items],
            watch=draft.watch.strip() or None,
        )
        s.add(briefing)
        await s.commit()
    log.info("news_briefing_created", id=briefing.id, stories=len(draft.items), trigger=trigger)
    return briefing


async def briefing_out(session: AsyncSession, b: Briefing) -> BriefingOut:
    ids = [int(i["article_id"]) for i in b.items]
    articles = {a.id: a for a in [await store.get_article(session, i) for i in ids] if a}
    items = [
        BriefingItemOut(
            article_id=a.id,
            take=i["take"],
            title=a.title,
            url=a.url,
            source_name=a.source.name,
            importance=a.importance,
        )
        for i in b.items
        if (a := articles.get(int(i["article_id"])))
    ]
    return BriefingOut(
        id=b.id,
        created_at=b.created_at,
        provider=b.provider,
        trigger=b.trigger,
        headline=b.headline,
        overview=b.overview,
        items=items,
        watch=b.watch,
    )
