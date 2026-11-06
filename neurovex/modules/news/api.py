"""REST + live API for the news module, mounted at /api/news."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from neurovex.core.db import utcnow
from neurovex.core.live import sse_stream
from neurovex.modules.news import store
from neurovex.modules.news.briefing import briefing_out
from neurovex.modules.news.catalog import CATALOG
from neurovex.modules.news.models import InteractionAction, Interest, InterestKind, Source
from neurovex.modules.news.module import NewsModule
from neurovex.modules.news.personalize import Profile
from neurovex.modules.news.schemas import (
    ArticleOut,
    ArticlePage,
    BriefingOut,
    CatalogFeed,
    CatalogGroup,
    CategoryCount,
    FeedbackIn,
    FlagIn,
    InterestIn,
    InterestOut,
    LearnedOut,
    NewsStatus,
    ProfileOut,
    RunOut,
    SourceCreate,
    SourceOut,
    SourceUpdate,
    XSourceCreate,
)
from neurovex.modules.news.summarizer import CATEGORIES
from neurovex.modules.news.urls import is_http_url
from neurovex.modules.news.xsource import is_x_url, parse_handle, source_url

router = APIRouter(prefix="/api/news", tags=["news"])


def get_news(request: Request) -> NewsModule:
    return request.app.state.news


async def get_session(news: Annotated[NewsModule, Depends(get_news)]):
    async with news.sessions() as s:
        yield s


News = Annotated[NewsModule, Depends(get_news)]
Session = Annotated[AsyncSession, Depends(get_session)]


async def _article_or_404(s: AsyncSession, article_id: int):  # type: ignore[no-untyped-def]
    article = await store.get_article(s, article_id)
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    return article


async def _scored(s: AsyncSession, article) -> ArticleOut:  # type: ignore[no-untyped-def]
    profile = await store.load_profile(s)
    return store.to_article_out(article, store.evaluate(profile, article))


# --- feed -------------------------------------------------------------------------------


@router.get("/articles", response_model=ArticlePage)
async def list_articles(
    s: Session,
    sort: store.Sort = "for_you",
    category: str | None = None,
    topic: Annotated[str | None, Query(max_length=60)] = None,
    min_importance: Annotated[int | None, Query(ge=1, le=10)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    since_hours: Annotated[int | None, Query(ge=1, le=24 * 90)] = None,
    bookmarked: bool | None = None,
    unread: bool = False,
    source_id: int | None = None,
    x_only: bool = False,
    include_hidden: bool = False,
    show_muted: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArticlePage:
    profile = await store.load_profile(s)
    items, total = await store.list_articles(
        s,
        profile,
        sort=sort,
        limit=limit,
        offset=offset,
        last_seen=await store.get_last_seen(s),
        show_muted=show_muted,
        category=category,
        topic=topic,
        min_importance=min_importance,
        q=q,
        since_hours=since_hours,
        bookmarked=bookmarked,
        unread=unread,
        source_id=source_id,
        x_ids=await store.x_source_ids(s) if x_only else None,
        include_hidden=include_hidden,
    )
    return ArticlePage(items=items, total=total, limit=limit, offset=offset)


@router.get("/articles/{article_id}", response_model=ArticleOut)
async def get_article(article_id: int, s: Session) -> ArticleOut:
    return await _scored(s, await _article_or_404(s, article_id))


@router.post("/articles/{article_id}/bookmark", response_model=ArticleOut)
async def bookmark(article_id: int, body: FlagIn, s: Session) -> ArticleOut:
    article = await _article_or_404(s, article_id)
    if body.value and not article.bookmarked:
        await store.record_interaction(s, article, InteractionAction.SAVE)
    article.bookmarked = body.value
    await s.commit()
    return await _scored(s, article)


@router.post("/articles/{article_id}/read", response_model=ArticleOut)
async def mark_read(article_id: int, body: FlagIn, s: Session) -> ArticleOut:
    article = await _article_or_404(s, article_id)
    article.read_at = utcnow() if body.value else None
    await s.commit()
    return await _scored(s, article)


@router.post("/articles/{article_id}/feedback", response_model=ArticleOut)
async def feedback(article_id: int, body: FeedbackIn, s: Session) -> ArticleOut:
    """Teach the ranking: open / save / more / less / hide."""
    article = await _article_or_404(s, article_id)
    await store.record_interaction(s, article, body.action)
    await s.commit()
    return await _scored(s, article)


@router.post("/articles/{article_id}/unhide", response_model=ArticleOut)
async def unhide(article_id: int, s: Session) -> ArticleOut:
    article = await _article_or_404(s, article_id)
    article.hidden_at = None
    await s.commit()
    return await _scored(s, article)


@router.get("/digest", response_model=list[ArticleOut])
async def digest(
    s: Session,
    hours: Annotated[int, Query(ge=1, le=24 * 7)] = 24,
    limit: Annotated[int, Query(ge=1, le=50)] = 7,
) -> list[ArticleOut]:
    return await store.digest(s, await store.load_profile(s), hours=hours, limit=limit)


@router.get("/categories", response_model=list[CategoryCount])
async def categories(
    s: Session, source_id: int | None = None, x_only: bool = False
) -> list[CategoryCount]:
    ids = [source_id] if source_id else await store.x_source_ids(s) if x_only else None
    counts = dict(await store.category_counts(s, source_ids=ids))
    return [CategoryCount(name=c, count=counts.get(c, 0)) for c in CATEGORIES]


@router.get("/topics/trending", response_model=list[CategoryCount])
async def trending(
    s: Session, hours: Annotated[int, Query(ge=6, le=24 * 7)] = 48
) -> list[CategoryCount]:
    return [CategoryCount(name=n, count=c) for n, c in await store.trending_topics(s, hours)]


# --- status & live ------------------------------------------------------------------------


@router.get("/status", response_model=NewsStatus)
async def news_status(news: News, s: Session) -> NewsStatus:
    runs = await store.list_runs(s, limit=1)
    last_seen = await store.get_last_seen(s)
    latest = await store.latest_briefing(s)
    return NewsStatus(
        running=news.pipeline.running,
        provider=news.pipeline.provider_name,
        auto_refresh=news.settings.news_auto_refresh,
        refresh_minutes=news.settings.news_refresh_minutes,
        next_run_at=news.next_run_at,
        last_run=runs[0] if runs else None,
        counts=await store.status_counts(s),
        last_seen_at=last_seen,
        new_since_seen=await store.count_new_since(s, last_seen),
        briefing_at=latest.created_at if latest else None,
        briefing_running=news.pipeline.briefing_running,
    )


@router.post("/seen", status_code=status.HTTP_204_NO_CONTENT)
async def mark_seen(s: Session) -> None:
    """Record that you've looked at the feed (powers 'new since your last visit')."""
    await store.set_state(s, "last_seen_at", utcnow().isoformat())
    await s.commit()


@router.get("/events")
async def events(news: News, request: Request) -> StreamingResponse:
    """Server-Sent Events: run progress, new articles, breaking news, briefings."""
    return StreamingResponse(
        sse_stream(news.live, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh(news: News) -> dict[str, str]:
    if not news.trigger_refresh():
        raise HTTPException(status.HTTP_409_CONFLICT, "A refresh is already running")
    return {"status": "started"}


@router.get("/runs", response_model=list[RunOut])
async def runs(s: Session, limit: Annotated[int, Query(ge=1, le=100)] = 10) -> list[RunOut]:
    return await store.list_runs(s, limit=limit)


# --- briefing -----------------------------------------------------------------------------


@router.get("/briefing", response_model=BriefingOut | None)
async def get_briefing(s: Session) -> BriefingOut | None:
    """The latest briefing, or null if none has been written yet."""
    latest = await store.latest_briefing(s)
    return await briefing_out(s, latest) if latest else None


@router.post("/briefing", status_code=status.HTTP_202_ACCEPTED)
async def new_briefing(news: News) -> dict[str, str]:
    if news.pipeline.provider_name == "none" and await news.pipeline.detect_provider() == "none":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No AI provider available: add a free key (nvx llm setup)",
        )
    if not news.trigger_briefing():
        raise HTTPException(status.HTTP_409_CONFLICT, "A briefing is already being written")
    return {"status": "started"}


# --- personal profile -----------------------------------------------------------------------


def _interest_out(i: Interest, profile: Profile) -> InterestOut:
    label = i.value
    if i.kind == InterestKind.SOURCE and i.value_key.isdigit():
        label = profile.source_names.get(int(i.value_key), i.value)
    return InterestOut(
        id=i.id, kind=i.kind, value=i.value, label=label,
        mode="follow" if i.weight > 0 else "mute",
    )  # fmt: skip


@router.get("/profile", response_model=ProfileOut)
async def get_profile(s: Session) -> ProfileOut:
    profile = await store.load_profile(s)
    interests = await store.list_interests(s)

    def learned(positive: bool) -> list[LearnedOut]:
        return [
            LearnedOut(key=k, label=label, kind=k.partition(":")[0], affinity=a)
            for k, label, a in profile.top_learned(10, positive=positive)
        ]

    return ProfileOut(
        following=[_interest_out(i, profile) for i in interests if i.weight > 0],
        muted=[_interest_out(i, profile) for i in interests if i.weight < 0],
        learned_likes=learned(True),
        learned_dislikes=learned(False),
        interactions=await store.interaction_count(s),
        trending_topics=[CategoryCount(name=n, count=c) for n, c in await store.trending_topics(s)],
    )


@router.post("/interests", response_model=InterestOut, status_code=status.HTTP_201_CREATED)
async def add_interest(body: InterestIn, s: Session) -> InterestOut:
    value = body.value.strip()
    if body.kind is InterestKind.SOURCE and not (
        value.isdigit() and await s.get(Source, int(value))
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown source id")
    if body.kind is InterestKind.CATEGORY and value not in CATEGORIES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown category")
    interest = await store.set_interest(s, body.kind, value, 1.0 if body.mode == "follow" else -1.0)
    await s.commit()
    return _interest_out(interest, await store.load_profile(s))


@router.delete("/interests/{interest_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_interest(interest_id: int, s: Session) -> None:
    interest = await s.get(Interest, interest_id)
    if interest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interest not found")
    await s.delete(interest)
    await s.commit()


@router.post("/profile/reset-learning")
async def reset_learning(s: Session) -> dict[str, int]:
    removed = await store.reset_learning(s)
    await s.commit()
    return {"removed": removed}


# --- sources -------------------------------------------------------------------------------


@router.get("/catalog", response_model=list[CatalogGroup])
async def catalog(s: Session) -> list[CatalogGroup]:
    added = {src.url for src in await store.list_sources(s)}
    return [
        CatalogGroup(
            name=group,
            feeds=[
                CatalogFeed(name=f["name"], url=f["url"], added=f["url"] in added) for f in feeds
            ],
        )
        for group, feeds in CATALOG.items()
    ]


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(s: Session) -> list[SourceOut]:
    return await store.list_sources(s)


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def add_source(body: SourceCreate, s: Session) -> SourceOut:
    url = body.url.strip()
    if not is_http_url(url):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "URL must be http(s)")
    if is_x_url(url):  # x.com pages aren't feeds; profiles have their own route
        if handle := parse_handle(url):
            return await _add_x_account(s, handle, body.category)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Use an X profile address, like x.com/OpenAI"
        )
    if await store.get_source_by_url(s, url):
        raise HTTPException(status.HTTP_409_CONFLICT, "That feed is already added")
    src = Source(name=body.name.strip(), url=url, category=body.category, enabled=True)
    s.add(src)
    await s.commit()
    return SourceOut.model_validate(src)


async def _add_x_account(s: AsyncSession, handle: str, category: str | None) -> SourceOut:
    url = source_url(handle)
    if await store.get_source_by_url(s, url):
        raise HTTPException(status.HTTP_409_CONFLICT, f"@{handle} is already added")
    src = Source(name=f"@{handle}", url=url, category=category, enabled=True)
    s.add(src)
    await s.commit()
    return SourceOut.model_validate(src)


@router.post("/sources/x", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def add_x_source(body: XSourceCreate, s: Session) -> SourceOut:
    """Follow an X account. Its posts arrive on the next refresh."""
    handle = parse_handle(body.handle)
    if handle is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "That isn't an X account: use @name or an address like x.com/name",
        )
    return await _add_x_account(s, handle, body.category)


@router.patch("/sources/{source_id}", response_model=SourceOut)
async def update_source(source_id: int, body: SourceUpdate, s: Session) -> SourceOut:
    src = await s.get(Source, source_id)
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(src, field, value)
    await s.commit()
    return SourceOut.model_validate(src)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: int, s: Session) -> None:
    src = await s.get(Source, source_id)
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    await s.delete(src)
    await s.commit()
