from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from neurovex.core.db import create_engine, init_db
from neurovex.modules.news import briefing, store
from neurovex.modules.news.models import Article, ArticleStatus, Briefing, InterestKind, Source
from neurovex.modules.news.pipeline import NewsPipeline
from tests.conftest import FakeSummarizer, hours_ago, rss

FEED = "https://feed.test/rss"


async def setup(settings, sessions, summarizer, *, follows=(), mutes=(), **overrides):
    settings = settings.model_copy(update=overrides)
    async with sessions() as s:
        s.add(Source(name="Feed", url=FEED))
        for v in follows:
            await store.set_interest(s, InterestKind.TOPIC, v, 1.0)
        for v in mutes:
            await store.set_interest(s, InterestKind.TOPIC, v, -1.0)
        await s.commit()

    async def factory(_s, _h):
        return summarizer

    http = httpx.AsyncClient()
    return NewsPipeline(settings, sessions, http, factory), http


@respx.mock
async def test_followed_topics_are_summarized_first(settings, sessions):
    respx.get(FEED).respond(200, content=rss(
        ("Generic gadget launch", "https://n.test/1", hours_ago(0.5)),
        ("Another phone review", "https://n.test/2", hours_ago(0.6)),
        ("Rust 2.0 ships with a new borrow checker", "https://n.test/3", hours_ago(20)),
    ))  # fmt: skip
    fake = FakeSummarizer()
    pipeline, http = await setup(
        settings, sessions, fake, follows=["Rust"], news_max_summaries_per_run=1
    )
    await pipeline.run()
    await http.aclose()
    assert [c.title for c in fake.calls] == ["Rust 2.0 ships with a new borrow checker"]


@respx.mock
async def test_muted_topics_are_never_summarized_or_listed(settings, sessions):
    respx.get(FEED).respond(200, content=rss(
        ("Crypto token soars", "https://n.test/c", hours_ago(1)),
        ("Linux kernel release", "https://n.test/l", hours_ago(1)),
    ))  # fmt: skip
    fake = FakeSummarizer()
    pipeline, http = await setup(settings, sessions, fake, mutes=["crypto"])
    await pipeline.run()
    await http.aclose()
    assert [c.title for c in fake.calls] == ["Linux kernel release"]
    async with sessions() as s:
        profile = await store.load_profile(s)
        items, total = await store.list_articles(s, profile)
        assert [a.title for a in items] == ["Linux kernel release"] and total == 1
        items, _ = await store.list_articles(s, profile, show_muted=True)
        assert len(items) == 2


@respx.mock
async def test_live_events_and_breaking(settings, sessions):
    respx.get(FEED).respond(200, content=rss(
        ("Critical NetScaler flaw exploited", "https://n.test/x", hours_ago(1)),
        ("Minor app update", "https://n.test/y", hours_ago(1)),
        ("Rust release notes", "https://n.test/z", hours_ago(1)),
    ))  # fmt: skip
    pipeline, http = await setup(settings, sessions, FakeSummarizer(), follows=["Rust"],
                                 news_briefing_auto=False)  # fmt: skip
    with pipeline.live.subscribe() as q:
        await pipeline.run()
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    await http.aclose()

    kinds = [e for e, _ in events]
    assert kinds[0] == "run" and events[0][1]["state"] == "started"
    assert kinds.count("article") == 3
    breaking = {d["title"] for e, d in events if e == "breaking"}
    # importance 9 for everyone; followed Rust (importance 7) for this reader.
    assert breaking == {"Critical NetScaler flaw exploited", "Rust release notes"}
    assert events[-1][0] == "run" and events[-1][1]["state"] == "finished"


@respx.mock
async def test_auto_briefing_after_run_then_waits_until_stale(settings, sessions):
    respx.get(FEED).respond(200, content=rss(
        *[(f"Story number {i}", f"https://n.test/{i}", hours_ago(i)) for i in range(1, 5)]
    ))  # fmt: skip
    fake = FakeSummarizer()
    pipeline, http = await setup(settings, sessions, fake, follows=["Rust"])
    with pipeline.live.subscribe() as q:
        await pipeline.run()
        await pipeline.run()  # nothing new + briefing not stale: no second briefing
        events = [q.get_nowait() for _ in range(q.qsize())]
    await http.aclose()

    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Briefing)) == 1
        b = await store.latest_briefing(s)
        out = await briefing.briefing_out(s, b)
    assert b.trigger == "auto" and len(out.items) == 4
    assert out.items[0].title.startswith("Story number") and out.items[0].take.startswith("Take on")
    assert fake.briefings[0].following == ["Rust"]
    assert ("briefing", "ready") in {(e, d.get("state")) for e, d in events}


async def test_briefing_needs_enough_stories(settings, sessions):
    with pytest.raises(briefing.NotEnoughStories):
        await briefing.generate_briefing(sessions, FakeSummarizer(), settings)


async def test_init_db_adds_new_nullable_columns(settings):
    settings.ensure_dirs()
    engine = create_engine(settings.db_url)
    async with engine.begin() as conn:  # an "old" schema without topics/hidden_at
        await conn.execute(text(
            "CREATE TABLE news_articles (id INTEGER PRIMARY KEY, source_id INTEGER, url TEXT, "
            "canonical_url TEXT UNIQUE, title TEXT, title_hash TEXT, author TEXT, published_at DATETIME, "
            "discovered_at DATETIME, excerpt TEXT, status TEXT, attempts INTEGER, last_error TEXT, "
            "duplicate_of_id INTEGER, summary TEXT, what_happened TEXT, why_it_matters TEXT, "
            "key_points JSON, categories JSON, importance INTEGER, summarized_by TEXT, "
            "processed_at DATETIME, bookmarked BOOLEAN, read_at DATETIME)"
        ))  # fmt: skip
    await init_db(engine)
    async with engine.connect() as conn:
        cols = {r[1] for r in await conn.execute(text("PRAGMA table_info(news_articles)"))}
    await engine.dispose()
    assert {"topics", "hidden_at"} <= cols


@respx.mock
async def test_hidden_articles_leave_the_feed_and_teach_the_ranking(settings, sessions):
    respx.get(FEED).respond(200, content=rss(
        ("Python 4 announced", "https://n.test/p", hours_ago(1)),
        ("Windows update notes", "https://n.test/w", hours_ago(1)),
    ))  # fmt: skip
    pipeline, http = await setup(settings, sessions, FakeSummarizer(), news_briefing_auto=False)
    await pipeline.run()
    await http.aclose()

    async with sessions() as s:
        win = await s.scalar(select(Article).where(Article.title.like("Windows%")))
        py = await s.scalar(select(Article).where(Article.title.like("Python%")))
        from neurovex.modules.news.models import InteractionAction

        await store.record_interaction(s, win, InteractionAction.HIDE)
        await store.record_interaction(s, py, InteractionAction.MORE)
        await s.commit()
        profile = await store.load_profile(s)
        items, _ = await store.list_articles(s, profile)
        assert [a.title for a in items] == ["Python 4 announced"]
        assert profile.learned["topic:python"] > 0 > profile.learned["topic:windows"]
        assert win.status == ArticleStatus.PROCESSED and win.hidden_at is not None


@respx.mock
async def test_rate_limited_providers_dont_burn_article_attempts(settings, sessions):
    from neurovex.modules.news.summarizer import ProvidersUnavailable

    class AllLimited(FakeSummarizer):
        async def summarize(self, article):
            self.calls.append(article)
            raise ProvidersUnavailable("All providers failed: groq: rate_limited", 30)

    respx.get(FEED).respond(
        200,
        content=rss(*[(f"Story {i}", f"https://n.test/s{i}", hours_ago(i)) for i in range(1, 6)]),
    )
    fake = AllLimited()
    pipeline, http = await setup(settings, sessions, fake, news_briefing_auto=False)
    run = await pipeline.run()
    await http.aclose()

    assert len(fake.calls) <= 4  # stops once providers are known to be down (4 in flight max)
    assert run.articles_failed == 0 and "AI providers unavailable" in (run.error or "")
    async with sessions() as s:
        rows = list(await s.scalars(select(Article)))
    assert all(a.status == ArticleStatus.DISCOVERED and a.attempts == 0 for a in rows)
