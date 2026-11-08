"""The news pipeline: ingest feeds, dedupe, summarize (personal priority), brief, prune.

One run at a time (guarded by a lock). Every article is written in its own short
transaction, so a crash mid-run never loses finished work: anything not yet
summarized stays DISCOVERED and is picked up by the next run.

What gets summarized first is decided by your personal score, so with a slow
local model the stories you care about are never stuck behind the backlog.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.core.live import Broadcaster
from neurovex.core.logging import get_logger
from neurovex.modules.news import briefing, fetcher, store
from neurovex.modules.news.models import Article, ArticleStatus, Run, Source
from neurovex.modules.news.personalize import Evaluation, Profile
from neurovex.modules.news.schemas import RunOut
from neurovex.modules.news.summarizer import (
    ArticleInput,
    ProvidersUnavailable,
    SummarizeError,
    Summarizer,
    SummarizerFactory,
)
from neurovex.modules.news.urls import canonicalize_url, title_fingerprint
from neurovex.modules.news.xsource import XReader, is_x_source, is_x_url, pick_due

log = get_logger(__name__)

THIN_EXCERPT_CHARS = 600  # below this, fetch the full page before summarizing
DUPLICATE_WINDOW = timedelta(days=3)
FEED_CONCURRENCY = 4
CLOUD_SUMMARY_CONCURRENCY = 4
QUEUE_CANDIDATES = 600


class AlreadyRunning(Exception):
    pass


@dataclass
class _Counts:
    sources_checked: int = 0
    sources_failed: int = 0
    new: int = 0
    duplicate: int = 0
    summarized: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)
    providers_down: bool = False  # every LLM provider is rate limited / unavailable


class NewsPipeline:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        summarizer_factory: SummarizerFactory,
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.http = http
        self.x = XReader(settings, sessions, http)
        self.live = broadcaster or Broadcaster()
        self._summarizer_factory = summarizer_factory
        self._lock = asyncio.Lock()
        self.provider_name = "none"
        self.briefing_running = False

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def current_writer(self) -> Summarizer | None:
        writer = await self._summarizer_factory(self.settings, self.http)
        self.provider_name = writer.name if writer else "none"
        return writer

    async def detect_provider(self) -> str:
        await self.current_writer()
        return self.provider_name

    async def run(self, trigger: str = "manual") -> RunOut:
        if self._lock.locked():
            raise AlreadyRunning
        async with self._lock:
            return await self._run(trigger)

    async def make_briefing(self, trigger: str = "manual") -> int | None:
        """Generate a personal briefing now. Returns its id (None if no LLM is available)."""
        writer = await self.current_writer()
        if writer is None:
            return None
        self.briefing_running = True
        self.live.publish("briefing", {"state": "started"})
        try:
            b = await briefing.generate_briefing(self.sessions, writer, self.settings, trigger)
        except Exception as e:
            self.live.publish("briefing", {"state": "failed", "error": str(e)[:200]})
            raise
        finally:
            self.briefing_running = False
        self.live.publish("briefing", {"state": "ready", "id": b.id, "headline": b.headline})
        return b.id

    async def _run(self, trigger: str) -> RunOut:
        started = time.perf_counter()
        # Rebuilt every run so a newly added API key or pulled model takes effect.
        writer = await self.current_writer()

        async with self.sessions() as s:
            run = Run(trigger=trigger, provider=self.provider_name)
            s.add(run)
            await s.commit()
            run_id = run.id
        log.info("news_run_started", run_id=run_id, trigger=trigger, provider=self.provider_name)
        self.live.publish("run", {"state": "started", "run_id": run_id})

        counts = _Counts()
        status, error = "OK", None
        try:
            await self._ingest(counts)
            if counts.new:
                self.live.publish("run", {"state": "ingested", "run_id": run_id, "new": counts.new})
            if writer:
                async with self.sessions() as s:
                    profile = await store.load_profile(s)
                await self._summarize_pending(writer, profile, counts)
            async with self.sessions() as s:
                counts.pruned = await store.prune_old(s, self.settings.news_retention_days)
                await s.commit()
        except Exception as e:  # a run must always be recorded, even when it crashes
            status, error = "ERROR", repr(e)
            log.exception("news_run_crashed", run_id=run_id)

        if counts.errors and error is None:
            error = "; ".join(counts.errors[:5])
        async with self.sessions() as s:
            run = await s.get_one(Run, run_id)
            run.finished_at = utcnow()
            run.status = status
            run.sources_checked = counts.sources_checked
            run.sources_failed = counts.sources_failed
            run.articles_new = counts.new
            run.articles_duplicate = counts.duplicate
            run.articles_summarized = counts.summarized
            run.articles_skipped = counts.skipped
            run.articles_failed = counts.failed
            run.articles_pruned = counts.pruned
            run.duration_s = round(time.perf_counter() - started, 2)
            run.error = error
            await s.commit()
            out = RunOut.model_validate(run)
        log.info(
            "news_run_finished",
            run_id=run_id,
            status=status,
            new=counts.new,
            summarized=counts.summarized,
            skipped=counts.skipped,
            failed=counts.failed,
            duration_s=out.duration_s,
        )
        self.live.publish("run", {"state": "finished", **out.model_dump(mode="json")})

        if writer and status == "OK" and self.settings.news_briefing_auto:
            await self._maybe_auto_brief()
        return out

    async def _maybe_auto_brief(self) -> None:
        async with self.sessions() as s:
            due = await briefing.should_auto_brief(s, self.settings)
        if not due:
            return
        try:
            await self.make_briefing("auto")
        except Exception as e:  # a failed briefing never fails the run
            log.warning("news_auto_briefing_failed", error=str(e)[:300])

    # --- ingest ----------------------------------------------------------------

    async def _ingest(self, counts: _Counts) -> None:
        async with self.sessions() as s:
            sources = list(await s.scalars(select(Source).where(Source.enabled.is_(True))))
        sem = asyncio.Semaphore(FEED_CONCURRENCY)
        x_now = {s.id for s in pick_due([s for s in sources if is_x_source(s.url)],
                                        self.settings, utcnow())}  # fmt: skip

        async def fetch(src: Source) -> fetcher.FeedResult | Exception:
            try:
                if is_x_source(src.url):
                    # X reads queue on their own lock, so they never hold up the feeds' slots.
                    return await self.x.fetch(src) if src.id in x_now else self.x.not_due(src)
                async with sem:
                    return await fetcher.fetch_feed(self.http, src.url, src.etag, src.last_modified)
            except Exception as e:  # one broken feed must not stop the others
                return e

        results = await asyncio.gather(*(fetch(src) for src in sources))
        for src, result in zip(sources, results, strict=True):
            counts.sources_checked += 1
            async with self.sessions() as s:
                db_src = await s.get(Source, src.id)
                if db_src is None:  # removed while we were fetching
                    continue
                db_src.last_checked_at = utcnow()
                if isinstance(result, Exception):
                    counts.sources_failed += 1
                    db_src.last_error = _short_error(result)
                    counts.errors.append(f"{src.name}: {db_src.last_error}")
                    log.warning("news_feed_failed", source=src.name, error=db_src.last_error)
                else:
                    db_src.last_error = result.note
                    if not result.not_modified:
                        db_src.etag = result.etag
                        db_src.last_modified = result.last_modified
                        new, dup = await self._store_entries(s, db_src, result.entries)
                        counts.new += new
                        counts.duplicate += dup
                await s.commit()

    async def _store_entries(
        self, s: AsyncSession, src: Source, entries: list[fetcher.FeedEntry]
    ) -> tuple[int, int]:
        cutoff = utcnow() - timedelta(days=self.settings.news_max_age_days)
        fresh = [e for e in entries if e.published_at is None or e.published_at >= cutoff]
        fresh.sort(key=lambda e: e.published_at or utcnow(), reverse=True)

        new = dup = 0
        seen: set[str] = set()
        for entry in fresh:
            if new >= self.settings.news_max_new_per_source:
                break
            canonical = canonicalize_url(entry.url)
            if canonical in seen:
                continue
            seen.add(canonical)
            if await s.scalar(select(Article.id).where(Article.canonical_url == canonical)):
                continue

            title_hash = title_fingerprint(entry.title)
            original_id = await s.scalar(
                select(Article.id)
                .where(Article.title_hash == title_hash)
                .where(Article.status != ArticleStatus.DUPLICATE.value)
                .where(Article.discovered_at >= utcnow() - DUPLICATE_WINDOW)
                .limit(1)
            )
            s.add(
                Article(
                    source_id=src.id,
                    url=entry.url,
                    canonical_url=canonical,
                    title=entry.title,
                    title_hash=title_hash,
                    author=(entry.author or "")[:300] or None,
                    published_at=entry.published_at,
                    excerpt=entry.excerpt,
                    status=(
                        ArticleStatus.DUPLICATE if original_id else ArticleStatus.DISCOVERED
                    ).value,
                    duplicate_of_id=original_id,
                )
            )
            await s.flush()  # make it visible to the dedupe queries for later entries
            if original_id:
                dup += 1
            else:
                new += 1
        return new, dup

    # --- summarize -------------------------------------------------------------

    async def summarize_queue(self, profile: Profile, limit: int) -> list[int]:
        """Pending article ids, most relevant to *you* first; muted ones never."""
        cutoff = utcnow() - timedelta(days=self.settings.news_max_age_days)
        async with self.sessions() as s:
            rows = list(
                await s.scalars(
                    select(Article)
                    .where(
                        Article.status.in_(
                            [ArticleStatus.DISCOVERED.value, ArticleStatus.FAILED.value]
                        )
                    )
                    .where(Article.attempts < self.settings.news_max_attempts)
                    .where(Article.hidden_at.is_(None))
                    .where(store.sort_key() >= cutoff)
                    .order_by(store.sort_key().desc())
                    .limit(QUEUE_CANDIDATES)
                )
            )
        now = utcnow()
        scored = [(a, store.evaluate(profile, a, now)) for a in rows]
        scored = [(a, ev) for a, ev in scored if not ev.muted]
        scored.sort(key=lambda p: p[1].score, reverse=True)
        return [a.id for a, _ in scored[:limit]]

    async def _summarize_pending(
        self, writer: Summarizer, profile: Profile, counts: _Counts
    ) -> None:
        limit = (
            self.settings.news_max_summaries_per_run_local
            if writer.is_local
            else self.settings.news_max_summaries_per_run
        )
        if limit <= 0:
            return
        ids = await self.summarize_queue(profile, limit)
        # Local CPU inference gains nothing from parallel requests.
        sem = asyncio.Semaphore(1 if writer.is_local else CLOUD_SUMMARY_CONCURRENCY)

        async def one(article_id: int) -> None:
            async with sem:
                if counts.providers_down:  # don't hammer APIs that just said "slow down"
                    return
                await self._summarize_one(writer, profile, article_id, counts)

        await asyncio.gather(*(one(i) for i in ids))

    async def _summarize_one(
        self, writer: Summarizer, profile: Profile, article_id: int, counts: _Counts
    ) -> None:
        async with self.sessions() as s:
            article = await s.get(Article, article_id, options=[selectinload(Article.source)])
            if article is None:
                return
            text = article.excerpt or ""
            thin = len(text) < THIN_EXCERPT_CHARS and not is_x_url(article.url)
            if thin and self.settings.news_fetch_full_text:
                page = await fetcher.fetch_article_text(self.http, article.url)
                if page and len(page) > len(text):
                    text = page
            item = ArticleInput(
                source_name=article.source.name,
                title=article.title,
                url=article.url,
                published_at=article.published_at,
                text=text or "(no article text available; summarize from the title only)",
            )

            article.attempts += 1
            try:
                result = await writer.summarize(item)
            except ProvidersUnavailable as e:
                # Not this article's fault: keep it queued and don't use up an attempt.
                article.attempts -= 1
                if not counts.providers_down:
                    counts.providers_down = True
                    counts.errors.append(f"AI providers unavailable: {str(e)[:200]}")
                    log.warning("news_providers_unavailable", error=str(e)[:300])
                await s.commit()
                return
            except SummarizeError as e:
                article.status = ArticleStatus.FAILED.value
                article.last_error = str(e)[:500]
                counts.failed += 1
                log.warning("news_summary_failed", article_id=article_id, error=article.last_error)
                await s.commit()
                return

            article.title = result.title.strip()[:500] or article.title
            article.summary = result.summary
            article.what_happened = result.what_happened
            article.why_it_matters = result.why_it_matters
            article.key_points = result.key_points
            article.categories = [c.value for c in result.categories]
            article.topics = result.topics
            article.importance = result.importance if result.relevant else min(result.importance, 2)
            article.summarized_by = result.model or writer.name
            article.processed_at = utcnow()
            article.last_error = None
            article.status = (
                ArticleStatus.PROCESSED if result.relevant else ArticleStatus.SKIPPED
            ).value
            if result.relevant:
                counts.summarized += 1
            else:
                counts.skipped += 1
            await s.commit()

            if result.relevant:
                ev = store.evaluate(profile, article)
                self._announce(article, ev)

    def _announce(self, article: Article, ev: Evaluation) -> None:
        payload = {
            "id": article.id,
            "title": article.title,
            "source": article.source.name,
            "importance": article.importance,
            "score": ev.score,
            "reasons": ev.reasons,
            "url": article.url,
        }
        if ev.muted:
            return
        self.live.publish("article", payload)
        if is_breaking(article, ev, self.settings):
            self.live.publish("breaking", payload)


def is_breaking(article: Article, ev: Evaluation, settings: Settings) -> bool:
    """Fresh and either critical for everyone, or notable and something you follow."""
    if ev.muted or article.importance is None:
        return False
    age = utcnow() - store.article_when(article)
    if age > timedelta(hours=settings.news_breaking_max_age_hours):
        return False
    return article.importance >= settings.news_breaking_importance or (
        ev.followed and article.importance >= 6
    )


def _short_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.TimeoutException):
        return "timed out"
    if isinstance(e, httpx.HTTPError):
        return type(e).__name__
    return str(e)[:300] or type(e).__name__
