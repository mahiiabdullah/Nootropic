from __future__ import annotations

from datetime import timedelta

import httpx
import respx
from sqlalchemy import select, update

from neurovex.core.db import utcnow
from neurovex.modules.news import store
from neurovex.modules.news.models import Article, ArticleStatus, Run, Source
from neurovex.modules.news.pipeline import NewsPipeline
from tests.conftest import FakeSummarizer, hours_ago, rss

FEED_A = "https://a.test/rss"
FEED_B = "https://b.test/rss"


async def make_pipeline(settings, sessions, summarizer: FakeSummarizer | None):
    async with sessions() as s:
        s.add_all([Source(name="Alpha", url=FEED_A), Source(name="Beta", url=FEED_B)])
        await s.commit()

    async def factory(_settings, _http):
        return summarizer

    http = httpx.AsyncClient()
    return NewsPipeline(settings, sessions, http, factory), http


async def articles(sessions) -> list[Article]:
    async with sessions() as s:
        return list(await s.scalars(select(Article).order_by(Article.id)))


@respx.mock
async def test_full_run_summarizes_and_classifies(settings, sessions, fake_summarizer):
    respx.get(FEED_A).respond(200, content=rss(
        ("New AI model released", "https://news.test/ai?utm_source=rss", hours_ago(1)),
        ("Laptop deal of the day", "https://news.test/deal", hours_ago(2)),
        ("Boom goes the parser", "https://news.test/boom", hours_ago(3)),
    ))  # fmt: skip
    respx.get(FEED_B).respond(200, content=rss(
        ("New AI model released!", "https://other.test/same-story", hours_ago(1)),
        ("Ancient story", "https://other.test/old", hours_ago(24 * 10)),
    ))  # fmt: skip
    pipeline, http = await make_pipeline(settings, sessions, fake_summarizer)

    run = await pipeline.run("manual")
    await http.aclose()

    assert run.status == "OK" and run.provider == "fake-model"
    assert (run.sources_checked, run.sources_failed) == (2, 0)
    assert run.articles_new == 3  # the 10-day-old story is outside max_age_days
    assert run.articles_duplicate == 1  # same title from another source
    assert (run.articles_summarized, run.articles_skipped, run.articles_failed) == (1, 1, 1)

    by_title = {a.title: a for a in await articles(sessions)}
    ai = by_title["New AI model released"]
    assert ai.status == ArticleStatus.PROCESSED and ai.categories == ["AI"] and ai.importance == 7
    assert ai.canonical_url == "https://news.test/ai"
    assert by_title["Laptop deal of the day"].status == ArticleStatus.SKIPPED
    assert by_title["Laptop deal of the day"].importance <= 2
    boom = by_title["Boom goes the parser"]
    assert (
        boom.status == ArticleStatus.FAILED and boom.attempts == 1 and "exploded" in boom.last_error
    )
    dup = by_title["New AI model released!"]
    assert dup.status == ArticleStatus.DUPLICATE and dup.duplicate_of_id == ai.id
    # Duplicates are never sent to the model.
    assert all(c.title != "New AI model released!" for c in fake_summarizer.calls)


@respx.mock
async def test_second_run_dedupes_and_retries_failures(settings, sessions, fake_summarizer):
    respx.get(FEED_A).respond(200, content=rss(
        ("Story one", "https://news.test/1", hours_ago(1)),
        ("Boom story", "https://news.test/boom", hours_ago(1)),
    ))  # fmt: skip
    respx.get(FEED_B).respond(304)
    pipeline, http = await make_pipeline(settings, sessions, fake_summarizer)

    await pipeline.run()
    second = await pipeline.run()
    third = await pipeline.run()
    fourth = await pipeline.run()
    await http.aclose()

    assert second.articles_new == 0
    boom = next(a for a in await articles(sessions) if a.title == "Boom story")
    assert boom.attempts == settings.news_max_attempts  # retried, then given up
    assert fourth.articles_failed == 0 and third.articles_failed == 1
    assert len([c for c in fake_summarizer.calls if c.title == "Story one"]) == 1


@respx.mock
async def test_broken_feed_is_recorded_and_others_continue(settings, sessions, fake_summarizer):
    respx.get(FEED_A).respond(500)
    respx.get(FEED_B).respond(200, content=rss(("Fine", "https://b.test/fine", hours_ago(1))))
    pipeline, http = await make_pipeline(settings, sessions, fake_summarizer)

    run = await pipeline.run()
    await http.aclose()

    assert run.status == "OK" and run.sources_failed == 1 and run.articles_summarized == 1
    assert "Alpha: HTTP 500" in run.error
    async with sessions() as s:
        alpha = await s.scalar(select(Source).where(Source.url == FEED_A))
        assert alpha.last_error == "HTTP 500" and alpha.last_checked_at is not None


@respx.mock
async def test_without_summarizer_headlines_are_still_collected(settings, sessions):
    respx.get(FEED_A).respond(200, content=rss(("Only headline", "https://a.test/h", hours_ago(1))))
    respx.get(FEED_B).respond(304)
    pipeline, http = await make_pipeline(settings, sessions, None)

    run = await pipeline.run()
    await http.aclose()

    assert run.provider == "none" and run.articles_new == 1 and run.articles_summarized == 0
    [a] = await articles(sessions)
    assert a.status == ArticleStatus.DISCOVERED and a.attempts == 0


@respx.mock
async def test_prune_keeps_bookmarks(settings, sessions, fake_summarizer):
    respx.get(FEED_A).respond(200, content=rss(
        ("Keep me", "https://a.test/keep", hours_ago(1)),
        ("Drop me", "https://a.test/drop", hours_ago(1)),
    ))  # fmt: skip
    respx.get(FEED_B).respond(304)
    pipeline, http = await make_pipeline(settings, sessions, fake_summarizer)
    await pipeline.run()

    old = utcnow() - timedelta(days=settings.news_retention_days + 1)
    async with sessions() as s:
        await s.execute(update(Article).values(discovered_at=old))
        await s.execute(update(Article).where(Article.title == "Keep me").values(bookmarked=True))
        await s.commit()

    run = await pipeline.run()
    await http.aclose()
    assert run.articles_pruned == 1
    assert [a.title for a in await articles(sessions)] == ["Keep me"]


@respx.mock
async def test_queries_filter_and_digest(settings, sessions, fake_summarizer):
    respx.get(FEED_A).respond(200, content=rss(
        ("AI breakthrough", "https://a.test/ai", hours_ago(1)),
        ("Chip news", "https://a.test/chip", hours_ago(2)),
        ("Big deal sale", "https://a.test/deal", hours_ago(3)),
    ))  # fmt: skip
    respx.get(FEED_B).respond(304)
    pipeline, http = await make_pipeline(settings, sessions, fake_summarizer)
    await pipeline.run()
    await http.aclose()

    async with sessions() as s:
        items, total = await store.list_articles(s)
        assert total == 2  # off-topic article hidden by default
        items, _ = await store.list_articles(s, include_hidden=True)
        assert len(items) == 3
        items, _ = await store.list_articles(s, category="AI")
        assert [a.title for a in items] == ["AI breakthrough"]
        items, _ = await store.list_articles(s, q="chip")
        assert [a.title for a in items] == ["Chip news"]
        top = await store.digest(s, hours=24)
        assert {a.title for a in top} == {"AI breakthrough", "Chip news"}
        counts = await store.status_counts(s)
        assert counts["PROCESSED"] == 2 and counts["SKIPPED"] == 1 and counts["unread"] == 2
        assert dict(await store.category_counts(s)) == {"AI": 1, "Technology": 1}
        runs = await s.scalars(select(Run))
        assert len(list(runs)) == 1
