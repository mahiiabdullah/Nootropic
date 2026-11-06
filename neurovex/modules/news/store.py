"""Read/write queries for the news tables, including the personal layer."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import Select, String, cast, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from neurovex.core.db import utcnow
from neurovex.modules.news.models import (
    Article,
    ArticleStatus,
    Briefing,
    Interaction,
    InteractionAction,
    Interest,
    InterestKind,
    Run,
    Source,
    State,
)
from neurovex.modules.news.personalize import (
    Evaluation,
    InterestRule,
    Profile,
    article_terms,
    learn_affinities,
    learning_cutoff,
    norm,
)
from neurovex.modules.news.schemas import ArticleOut, RunOut, SourceOut
from neurovex.modules.news.sources import DEFAULT_SOURCES
from neurovex.modules.news.xsource import is_x_source

VISIBLE_STATUSES = (ArticleStatus.PROCESSED, ArticleStatus.DISCOVERED, ArticleStatus.FAILED)
MAX_CANDIDATES = 2500  # rows scored in Python per listing; plenty for a personal feed

Sort = Literal["for_you", "latest", "importance"]


def sort_key():  # newest first, falling back to discovery time for undated entries
    return func.coalesce(Article.published_at, Article.discovered_at)


def article_when(a: Article) -> datetime:
    return a.published_at or a.discovered_at


def evaluate(profile: Profile, a: Article, now: datetime | None = None) -> Evaluation:
    return profile.evaluate(
        title=a.title,
        text=a.summary or a.excerpt,
        topics=a.topics,
        categories=a.categories,
        source_id=a.source_id,
        importance=a.importance,
        when=article_when(a),
        now=now,
    )


def to_article_out(
    a: Article, ev: Evaluation | None = None, last_seen: datetime | None = None
) -> ArticleOut:
    return ArticleOut(
        id=a.id,
        source_id=a.source_id,
        source_name=a.source.name if a.source else "",
        url=a.url,
        title=a.title,
        author=a.author,
        published_at=a.published_at,
        discovered_at=a.discovered_at,
        status=a.status,
        excerpt=(a.excerpt[:600] if a.excerpt else None),
        summary=a.summary,
        what_happened=a.what_happened,
        why_it_matters=a.why_it_matters,
        key_points=a.key_points,
        categories=a.categories,
        topics=a.topics,
        importance=a.importance,
        summarized_by=a.summarized_by,
        bookmarked=a.bookmarked,
        read=a.read_at is not None,
        hidden=a.hidden_at is not None,
        last_error=a.last_error,
        score=ev.score if ev else None,
        reasons=ev.reasons if ev else [],
        followed=ev.followed if ev else False,
        is_new=bool(last_seen and a.discovered_at > last_seen),
    )


def _filtered(
    *,
    category: str | None = None,
    topic: str | None = None,
    min_importance: int | None = None,
    q: str | None = None,
    since_hours: int | None = None,
    bookmarked: bool | None = None,
    unread: bool | None = None,
    source_id: int | None = None,
    x_ids: list[int] | None = None,
    include_hidden: bool = False,
) -> Select[tuple[Article]]:
    stmt = select(Article)
    if not include_hidden:
        # Asking for X means all of it: the AI's "not relevant" call doesn't hide those posts.
        shown = VISIBLE_STATUSES if x_ids is None else (*VISIBLE_STATUSES, ArticleStatus.SKIPPED)
        stmt = stmt.where(Article.status.in_([s.value for s in shown]))
        stmt = stmt.where(Article.hidden_at.is_(None))
    if category:
        # JSON arrays are stored as text: match the quoted value (case-insensitive LIKE).
        stmt = stmt.where(cast(Article.categories, String).like(f'%"{category}"%'))
    if topic:
        stmt = stmt.where(cast(Article.topics, String).like(f'%"{topic}"%'))
    if min_importance:
        stmt = stmt.where(Article.importance >= min_importance)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Article.title.ilike(like), Article.summary.ilike(like), Article.excerpt.ilike(like))
        )
    if since_hours:
        stmt = stmt.where(sort_key() >= utcnow() - timedelta(hours=since_hours))
    if bookmarked is not None:
        stmt = stmt.where(Article.bookmarked.is_(bookmarked))
    if unread:
        stmt = stmt.where(Article.read_at.is_(None))
    if source_id:
        stmt = stmt.where(Article.source_id == source_id)
    if x_ids is not None:
        stmt = stmt.where(Article.source_id.in_(x_ids))
    return stmt


async def x_source_ids(session: AsyncSession) -> list[int]:
    rows = await session.execute(select(Source.id, Source.url))
    return [id_ for id_, url in rows if is_x_source(url)]


async def list_articles(
    session: AsyncSession,
    profile: Profile | None = None,
    *,
    sort: Sort = "for_you",
    limit: int = 50,
    offset: int = 0,
    last_seen: datetime | None = None,
    show_muted: bool = False,
    **filters,
) -> tuple[list[ArticleOut], int]:
    """Filter in SQL, then score, drop muted, sort and paginate in Python."""
    profile = profile or Profile()
    rows = await session.scalars(
        _filtered(**filters)
        .options(selectinload(Article.source))
        .order_by(sort_key().desc())
        .limit(MAX_CANDIDATES)
    )
    now = utcnow()
    scored = [(a, evaluate(profile, a, now)) for a in rows]
    if not show_muted and not filters.get("include_hidden"):
        scored = [(a, ev) for a, ev in scored if not ev.muted]

    if sort == "for_you":
        scored.sort(key=lambda p: p[1].score, reverse=True)
    elif sort == "importance":
        scored.sort(key=lambda p: (p[0].importance or 0, article_when(p[0])), reverse=True)
    # "latest" keeps the SQL order.
    page = scored[offset : offset + limit]
    return [to_article_out(a, ev, last_seen) for a, ev in page], len(scored)


async def get_article(session: AsyncSession, article_id: int) -> Article | None:
    return await session.get(Article, article_id, options=[selectinload(Article.source)])


async def top_for_you(
    session: AsyncSession,
    profile: Profile,
    *,
    hours: int = 24,
    limit: int = 7,
    since: datetime | None = None,
) -> list[tuple[Article, Evaluation]]:
    """Relevant, summarized stories from the window, ranked for this person."""
    cutoff = utcnow() - timedelta(hours=hours)
    stmt = (
        select(Article)
        .where(Article.status == ArticleStatus.PROCESSED.value)
        .where(Article.hidden_at.is_(None))
        .where(sort_key() >= cutoff)
        .options(selectinload(Article.source))
    )
    if since:
        stmt = stmt.where(Article.processed_at > since)
    now = utcnow()
    scored = [(a, evaluate(profile, a, now)) for a in await session.scalars(stmt)]
    scored = [p for p in scored if not p[1].muted]
    scored.sort(key=lambda p: p[1].score, reverse=True)
    return scored[:limit]


async def digest(
    session: AsyncSession, profile: Profile | None = None, hours: int = 24, limit: int = 7
) -> list[ArticleOut]:
    picked = await top_for_you(session, profile or Profile(), hours=hours, limit=limit)
    return [to_article_out(a, ev) for a, ev in picked]


async def category_counts(
    session: AsyncSession, days: int = 7, source_ids: list[int] | None = None
) -> list[tuple[str, int]]:
    stmt = (
        select(Article.categories)
        .where(Article.status == ArticleStatus.PROCESSED.value)
        .where(Article.hidden_at.is_(None))
        .where(sort_key() >= utcnow() - timedelta(days=days))
    )
    if source_ids is not None:
        stmt = stmt.where(Article.source_id.in_(source_ids))
    rows = await session.scalars(stmt)
    counter: Counter[str] = Counter()
    for cats in rows:
        counter.update(cats or [])
    return counter.most_common()


async def trending_topics(
    session: AsyncSession, hours: int = 48, limit: int = 12
) -> list[tuple[str, int]]:
    rows = await session.scalars(
        select(Article.topics)
        .where(Article.status == ArticleStatus.PROCESSED.value)
        .where(sort_key() >= utcnow() - timedelta(hours=hours))
    )
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for topics in rows:
        for t in topics or []:
            display.setdefault(norm(t), t)
            counter[norm(t)] += 1
    return [(display[k], n) for k, n in counter.most_common(limit) if n >= 2]


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Article.status, func.count()).group_by(Article.status))
    counts = {s.value: 0 for s in ArticleStatus}
    counts.update({status: n for status, n in rows.all()})
    counts["unread"] = (
        await session.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.status == ArticleStatus.PROCESSED.value)
            .where(Article.read_at.is_(None))
            .where(Article.hidden_at.is_(None))
        )
        or 0
    )
    return counts


async def count_new_since(session: AsyncSession, since: datetime | None) -> int:
    if since is None:
        return 0
    return (
        await session.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.status.in_([s.value for s in VISIBLE_STATUSES]))
            .where(Article.hidden_at.is_(None))
            .where(Article.discovered_at > since)
        )
        or 0
    )


# --- personal profile ----------------------------------------------------------------


async def load_profile(session: AsyncSession) -> Profile:
    interests = await session.scalars(select(Interest))
    rules = [
        InterestRule(id=i.id, kind=i.kind, value=i.value, key=i.value_key, weight=i.weight)
        for i in interests
    ]
    history = await session.execute(
        select(Interaction.action, Interaction.terms, Interaction.created_at).where(
            Interaction.created_at >= learning_cutoff()
        )
    )
    learned, labels = learn_affinities((a, t, w) for a, t, w in history.all())
    names = dict((await session.execute(select(Source.id, Source.name))).all())
    for r in rules:  # show source names instead of ids
        if r.kind == InterestKind.SOURCE and r.key.isdigit():
            labels.setdefault(f"src:{r.key}", names.get(int(r.key), r.value))
    return Profile(rules=rules, learned=learned, labels=labels, source_names=names)


async def record_interaction(
    session: AsyncSession, article: Article, action: InteractionAction
) -> None:
    """Store a learning signal and apply its side effect on the article."""
    now = utcnow()
    session.add(
        Interaction(
            article_id=article.id,
            action=action.value,
            terms=article_terms(article.topics, article.categories, article.source_id),
            created_at=now,
        )
    )
    if action is InteractionAction.OPEN and article.read_at is None:
        article.read_at = now
    elif action is InteractionAction.SAVE:
        article.bookmarked = True
    elif action is InteractionAction.HIDE:
        article.hidden_at = now


async def interaction_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Interaction)) or 0


async def reset_learning(session: AsyncSession) -> int:
    result = await session.execute(delete(Interaction))
    return result.rowcount or 0


def interest_key(kind: InterestKind, value: str) -> str:
    return value.strip() if kind is InterestKind.SOURCE else norm(value)


async def set_interest(
    session: AsyncSession, kind: InterestKind, value: str, weight: float
) -> Interest:
    """Follow (+1) or mute (-1). Re-following a muted term flips it."""
    key = interest_key(kind, value)
    existing = await session.scalar(
        select(Interest).where(Interest.kind == kind.value).where(Interest.value_key == key)
    )
    if existing:
        existing.weight = weight
        existing.value = value.strip()
        return existing
    interest = Interest(kind=kind.value, value=value.strip(), value_key=key, weight=weight)
    session.add(interest)
    await session.flush()
    return interest


async def list_interests(session: AsyncSession) -> list[Interest]:
    return list(await session.scalars(select(Interest).order_by(Interest.created_at)))


# --- state -----------------------------------------------------------------------------


async def get_state(session: AsyncSession, key: str) -> str | None:
    row = await session.get(State, key)
    return row.value if row else None


async def set_state(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(State, key)
    if row:
        row.value = value
    else:
        session.add(State(key=key, value=value))


async def get_last_seen(session: AsyncSession) -> datetime | None:
    raw = await get_state(session, "last_seen_at")
    return datetime.fromisoformat(raw) if raw else None


# --- briefings -------------------------------------------------------------------------


async def latest_briefing(session: AsyncSession) -> Briefing | None:
    return await session.scalar(select(Briefing).order_by(Briefing.id.desc()).limit(1))


# --- sources ---------------------------------------------------------------------------


async def seed_default_sources(session: AsyncSession) -> int:
    if await session.scalar(select(func.count()).select_from(Source)):
        return 0
    session.add_all(Source(**s) for s in DEFAULT_SOURCES)
    return len(DEFAULT_SOURCES)


async def list_sources(session: AsyncSession) -> list[SourceOut]:
    counts = dict(
        (
            await session.execute(
                select(Article.source_id, func.count()).group_by(Article.source_id)
            )
        ).all()
    )
    sources = await session.scalars(select(Source).order_by(Source.name))
    out = []
    for s in sources:
        item = SourceOut.model_validate(s)
        item.article_count = counts.get(s.id, 0)
        out.append(item)
    return out


async def get_source_by_url(session: AsyncSession, url: str) -> Source | None:
    return await session.scalar(select(Source).where(Source.url == url))


# --- runs -------------------------------------------------------------------------------


async def list_runs(session: AsyncSession, limit: int = 10) -> list[RunOut]:
    rows = await session.scalars(select(Run).order_by(Run.id.desc()).limit(limit))
    return [RunOut.model_validate(r) for r in rows]


async def prune_old(session: AsyncSession, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    result = await session.execute(
        delete(Article).where(Article.bookmarked.is_(False)).where(Article.discovered_at < cutoff)
    )
    return result.rowcount or 0
