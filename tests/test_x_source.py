from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from xtf import AllBackendsFailed, Tweet, UpstreamDown

from neurovex.core.db import utcnow
from neurovex.modules.news.models import Article, ArticleStatus, Source
from neurovex.modules.news.module import NewsModule
from neurovex.modules.news.xsource import (
    API,
    QUIET_FACTOR,
    TWITTER_EPOCH_MS,
    check_every,
    checks_per_run,
    from_xtf_tweet,
    is_own_post,
    is_x_source,
    is_x_url,
    merge_full_post,
    needs_full_post,
    new_posts,
    parse_handle,
    pick_due,
    post_to_entry,
    source_url,
)
from tests.conftest import FakeSummarizer
from tests.test_api import HEADERS, client, wait_for_idle  # noqa: F401  (fixture)

STATUSES = f"{API}/profile/OpenAI/statuses"


def id_from(ago: timedelta) -> str:
    """A post id made `ago` before now (ids carry their creation time)."""
    return str((int((utcnow() - ago).timestamp() * 1000) - TWITTER_EPOCH_MS) << 22)


RECENT_ID = id_from(timedelta(0))  # a post from now


def post(id_: str, text: str, **kw) -> dict:
    return {"id": id_, "text": text, "created_timestamp": int(utcnow().timestamp()), **kw}


REPLY = {"replying_to": {"screen_name": "OpenAI", "status": "1"}}
REPOST = {"reposted_by": {"id": "1", "name": "OpenAI"}}


@pytest.fixture
async def news(settings, sessions):
    async def factory(_s, _h):
        return FakeSummarizer()

    module = NewsModule(settings, sessions, factory)
    async with sessions() as s:
        s.add(Source(name="@OpenAI", url="https://x.com/OpenAI"))
        await s.commit()
    yield module
    await module.stop()


async def _source(sessions) -> Source:
    async with sessions() as s:
        return await s.scalar(select(Source).where(Source.name == "@OpenAI"))


# --- pure pieces ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "handle"),
    [
        ("@OpenAI", "OpenAI"),
        ("OpenAI", "OpenAI"),
        (" https://x.com/OpenAI ", "OpenAI"),
        ("https://twitter.com/@openai/", "openai"),
        ("https://mobile.twitter.com/openai?s=20", "openai"),
        ("https://x.com/openai/status/123", None),
        ("https://x.com/home", None),
        ("https://x.com/search", None),
        ("not a handle!", None),
        ("this_name_is_far_too_long_for_x", None),
        ("", None),
    ],
)
def test_parse_handle(raw, handle):
    assert parse_handle(raw) == handle


def test_source_urls_are_recognized():
    assert source_url("OpenAI") == "https://x.com/openai" and is_x_source(source_url("OpenAI"))
    assert not is_x_source("https://x.com/openai/status/1")
    assert not is_x_source("https://example.com/feed.xml")
    assert is_x_url("https://x.com/openai/status/1") and not is_x_url("https://notx.com/a")


def test_only_the_accounts_own_posts_count():
    assert is_own_post(post("1", "Hello"))
    assert is_own_post({**post("1", "Interesting"), "quote": post("2", "Big news")})
    assert not is_own_post(post("1", "Thread part two", **REPLY))
    assert not is_own_post(post("1", "Someone else's", **REPOST))
    assert not is_own_post(post("1", "   "))


def test_new_posts_filters_sorts_and_caps():
    posts = [post("5", "pinned, out of order"), post("9", "newest"), post("7", "reply", **REPLY),
             post("8", "repost", **REPOST), post("6", "middle"), post("3", "already seen")]  # fmt: skip
    assert [p["id"] for p in new_posts(posts, "3", 10)] == ["9", "6", "5"]
    assert [p["id"] for p in new_posts(posts, "3", 2)] == ["9", "6"]
    assert [p["id"] for p in new_posts(posts, None, 10)] == ["9", "6", "5", "3"]
    assert new_posts(posts, "9", 10) == []


def test_post_to_entry():
    entry = post_to_entry(
        {"id": "9", "text": "https://t.co/z\n\nNew model out today\nDetails below",
         "created_timestamp": 1793732205},
        "OpenAI",
    )  # fmt: skip
    assert entry.title == "New model out today" and entry.author == "@OpenAI"
    assert entry.url == "https://x.com/OpenAI/status/9"
    assert entry.published_at.isoformat() == "2026-11-03T18:56:45+00:00"
    assert post_to_entry({"id": "1", "text": "https://t.co/z"}, "a").title == "Post by @a"
    assert len(post_to_entry({"id": "2", "text": "x" * 300}, "a").title) == 120


def test_a_quoted_post_gives_a_short_one_its_meaning():
    quoted = {"text": "Rust 2.0 is out", "author": {"screen_name": "rustlang"}}
    entry = post_to_entry({"id": "1", "text": "Interesting", "quote": quoted}, "a")
    assert entry.title == "Interesting"
    assert entry.excerpt == "Interesting\n\nQuoting @rustlang: Rust 2.0 is out"


# --- reading ----------------------------------------------------------------------------------


@respx.mock
async def test_reads_new_posts_only_and_respects_the_interval(news, sessions):
    route = respx.get(STATUSES).mock(side_effect=[
        httpx.Response(200, json={"code": 200, "results": [
            post("100", "Older AI note"), post("101", "First big AI launch"),
            post("99", "Thread part two", **REPLY), post("98", "Someone else's", **REPOST),
        ]}),
        httpx.Response(200, json={"code": 200, "results": [
            post("102", "Second AI launch"), post("101", "First big AI launch"),
        ]}),
    ])  # fmt: skip
    run = await news.pipeline.run()
    assert run.sources_failed == 0 and run.articles_new == 2
    assert "Authorization" not in route.calls[0].request.headers  # no key, no login
    src = await _source(sessions)
    assert src.x_since_id == "101" and src.last_error is None

    await news.pipeline.run()  # within the interval: not even a request
    assert route.call_count == 1

    async def checked(ago: timedelta) -> None:
        async with sessions() as s:
            row = await s.get(Source, src.id)
            row.x_checked_at = utcnow() - ago
            await s.commit()

    # Its newest post (id 101) is years old, so it's a quiet account: checked every 6 hours.
    await checked(timedelta(minutes=31))
    await news.pipeline.run()
    assert route.call_count == 1
    await checked(timedelta(minutes=30 * QUIET_FACTOR + 1))
    run = await news.pipeline.run()
    assert route.call_count == 2 and run.articles_new == 1
    assert (await _source(sessions)).x_since_id == "102"


@pytest.mark.parametrize(
    ("last_post", "every_minutes"),
    [
        (timedelta(hours=3), 30),  # posts daily: every x_refresh_minutes
        (timedelta(days=2), 60),
        (timedelta(days=5), 180),
        (timedelta(days=20), 360),  # quiet for weeks: every 6 hours
        (None, 360),  # never seen a post of its own
    ],
)
def test_quiet_accounts_are_checked_less_often(settings, last_post, every_minutes):
    src = Source(name="@a", url="https://x.com/a", x_since_id=last_post and id_from(last_post))
    assert check_every(src, settings, utcnow()) == timedelta(minutes=every_minutes)


def test_due_accounts_are_spread_across_runs(settings):
    now = utcnow()
    assert checks_per_run(settings, 299) == 100  # 30-minute checks, 10-minute runs: a third each
    fresh = timedelta(hours=1)  # an account that posted an hour ago is due after 30 minutes
    accounts = [
        Source(id=i, name=f"@a{i}", url=f"https://x.com/a{i}", x_since_id=id_from(fresh),
               x_checked_at=None if i == 0 else now - timedelta(minutes=30 + i))
        for i in range(9)
    ]  # fmt: skip
    accounts.append(Source(id=99, name="@b", url="https://x.com/b", x_since_id=id_from(fresh),
                           x_checked_at=now - timedelta(minutes=5)))  # fmt: skip
    picked = pick_due(accounts, settings, now)
    # A share of 10 accounts is 4; never-checked first, then whoever waited longest; @b isn't due.
    assert [s.id for s in picked] == [0, 8, 7, 6]


@respx.mock
async def test_a_run_checks_only_its_share_of_accounts(settings, sessions):
    async def factory(_s, _h):
        return FakeSummarizer()

    module = NewsModule(settings, sessions, factory)
    async with sessions() as s:
        s.add_all(Source(name=f"@acct{i}", url=f"https://x.com/acct{i}") for i in range(6))
        await s.commit()
    route = respx.get(url__regex=rf"{API}/profile/acct\d/statuses").respond(
        200, json={"results": []}
    )
    await module.pipeline.run()
    assert route.call_count == 2  # 6 accounts, every 30 minutes, runs every 10: 2 per run
    await module.pipeline.run()
    assert route.call_count == 4  # the next run takes the next two, not the same ones
    async with sessions() as s:
        checked = await s.scalars(select(Source.name).where(Source.x_checked_at.is_not(None)))
        assert len(set(checked)) == 4
    await module.stop()


@respx.mock
async def test_posts_are_capped_per_check(settings, sessions):
    async def factory(_s, _h):
        return FakeSummarizer()

    capped = settings.model_copy(update={"x_max_posts_per_check": 2})
    module = NewsModule(capped, sessions, factory)
    async with sessions() as s:
        s.add(Source(name="@OpenAI", url="https://x.com/OpenAI"))
        await s.commit()
    results = [post(str(i), f"AI launch number {i}") for i in range(1, 8)]
    respx.get(STATUSES).respond(200, json={"code": 200, "results": results})
    assert (await module.pipeline.run()).articles_new == 2
    assert (await _source(sessions)).x_since_id == "7"
    await module.stop()


@respx.mock
async def test_summaries_use_the_post_and_never_fetch_the_x_page(news, sessions):
    respx.get(STATUSES).respond(200, json={"results": [post("7", "Rust 2.0 released")]})
    page = respx.get("https://x.com/OpenAI/status/7")
    await news.pipeline.run()
    assert not page.called
    async with sessions() as s:
        a = await s.scalar(select(Article))
    assert a.url == "https://x.com/OpenAI/status/7" and a.status == "PROCESSED"


@pytest.mark.parametrize(
    ("status", "says"),
    [
        (404, "No public X account named @OpenAI"),
        (429, "rate limiting"),
        (500, "HTTP 500"),
    ],
)
@respx.mock
async def test_failures_show_on_the_source_and_retry_next_time(news, sessions, status, says):
    route = respx.get(STATUSES).mock(side_effect=[
        httpx.Response(status, json={}),
        httpx.Response(200, json={"results": []}),
    ])  # fmt: skip
    run = await news.pipeline.run()
    src = await _source(sessions)
    assert run.sources_failed == 1 and says in src.last_error and src.x_checked_at is None
    await news.pipeline.run()
    src = await _source(sessions)
    assert route.call_count == 2 and src.last_error is None and src.x_checked_at is not None


@respx.mock
async def test_a_broken_reply_or_outage_does_not_stop_other_news(news, sessions):
    respx.get(STATUSES).mock(side_effect=httpx.ConnectError("down"))
    run = await news.pipeline.run()
    assert run.status != "FAILED" and run.sources_failed == 1
    assert "Could not reach the free X service" in (await _source(sessions)).last_error
    respx.get(STATUSES).mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    await news.pipeline.run()
    assert "unexpected" in (await _source(sessions)).last_error


# --- x-tweet-fetcher --------------------------------------------------------------------------


ARTICLE_TIMELINE = {
    "id": RECENT_ID,
    "text": "",
    "article": {"title": "Introducing Engram"},
}
ARTICLE_FULL = {
    "text": "",
    "is_article": True,
    "article": {
        "title": "Introducing Engram",
        "preview_text": "Short",
        "full_text": "The whole article.",
    },
}


def test_x_articles_count_and_get_their_text_from_xtf():
    assert is_own_post(ARTICLE_TIMELINE) and needs_full_post(ARTICLE_TIMELINE)
    assert needs_full_post(post("1", "Read it https://x.com/i/article/9"))
    assert not needs_full_post(
        post("1", "A long ordinary post " * 40)
    )  # timelines have these whole
    entry = post_to_entry(merge_full_post(ARTICLE_TIMELINE, ARTICLE_FULL), "EngramLab")
    assert entry.title == "Introducing Engram" and entry.excerpt == "The whole article."
    preview = {"article": {"title": "T", "preview_text": "Only a preview"}}
    assert merge_full_post(post("1", ""), preview)["text"] == "Only a preview"
    assert merge_full_post(post("1", "Kept"), {})["text"] == "Kept"


def test_post_ids_give_the_date_when_nothing_else_does():
    entry = post_to_entry({"id": "2117692475775258785", "text": "Hello"}, "a")
    assert entry.published_at.isoformat(timespec="seconds") == "2026-11-03T18:56:45+00:00"


def _tweet(id_="5", text="Launch day", author="@OpenAI", **kw) -> Tweet:
    return Tweet(tweet_id=id_, text=text, author=author, **kw)


def test_nitter_tweets_become_the_same_shape():
    own = from_xtf_tweet(_tweet(), "OpenAI")
    assert own == {"id": "5", "text": "Launch day"} and is_own_post(own)
    assert not is_own_post(from_xtf_tweet(_tweet(retweeted_by="@OpenAI"), "OpenAI"))
    assert not is_own_post(from_xtf_tweet(_tweet(author="@someone"), "OpenAI"))
    assert not is_own_post(from_xtf_tweet(_tweet(text="@someone thanks!"), "OpenAI"))
    quoted = from_xtf_tweet(_tweet(quoted_tweet=_tweet(text="Big", author="@rust")), "OpenAI")
    assert post_to_entry(quoted, "OpenAI").excerpt == "Launch day\n\nQuoting @rust: Big"


@respx.mock
async def test_an_x_article_is_read_in_full_through_xtf(news, sessions, monkeypatch):
    respx.get(STATUSES).respond(200, json={"results": [ARTICLE_TIMELINE, post("7", "Plain post")]})
    calls = []

    def fetch_tweet(handle, tweet_id):
        calls.append((handle, tweet_id))
        return ARTICLE_FULL

    monkeypatch.setattr(news.pipeline.x.xtf, "fetch_tweet", fetch_tweet)
    await news.pipeline.run()
    assert calls == [("OpenAI", ARTICLE_TIMELINE["id"])]  # the plain post didn't need it
    async with sessions() as s:
        a = await s.scalar(select(Article).where(Article.title == "Introducing Engram"))
    assert a.excerpt == "The whole article."


@respx.mock
async def test_xtf_failing_keeps_the_post(news, sessions, monkeypatch):
    respx.get(STATUSES).respond(200, json={"results": [
        {**ARTICLE_TIMELINE, "text": "Read my article"},
    ]})  # fmt: skip

    def broken(_handle, _tweet_id):
        raise UpstreamDown("down")

    monkeypatch.setattr(news.pipeline.x.xtf, "fetch_tweet", broken)
    run = await news.pipeline.run()
    assert run.sources_failed == 0 and run.articles_new == 1


@respx.mock
async def test_your_own_nitter_is_used_first_and_fxtwitter_covers_for_it(
    settings, sessions, monkeypatch
):
    async def factory(_s, _h):
        return FakeSummarizer()

    module = NewsModule(
        settings.model_copy(update={"x_nitter_url": "http://127.0.0.1:8788"}), sessions, factory
    )
    assert module.pipeline.x.xtf.nitter.instances == ["http://127.0.0.1:8788"]
    assert module.pipeline.x.xtf.mode == "nitter"  # never its browser backend
    async with sessions() as s:
        s.add(Source(name="@OpenAI", url="https://x.com/OpenAI"))
        await s.commit()
    newer = str(int(RECENT_ID) + 1)
    fx = respx.get(STATUSES).respond(200, json={"results": [post(newer, "From FxTwitter")]})

    monkeypatch.setattr(
        module.pipeline.x.xtf,
        "fetch_timeline",
        lambda _h, limit: [_tweet(RECENT_ID, "From Nitter")],
    )
    assert (await module.pipeline.run()).articles_new == 1
    assert not fx.called

    def down(_h, limit):
        raise AllBackendsFailed("all down", causes={})

    monkeypatch.setattr(module.pipeline.x.xtf, "fetch_timeline", down)
    async with sessions() as s:
        row = await s.scalar(select(Source))
        row.x_checked_at = None
        await s.commit()
    assert (await module.pipeline.run()).articles_new == 1
    assert fx.called
    await module.stop()


# --- API --------------------------------------------------------------------------------------


async def test_following_an_x_account(client):  # noqa: F811
    r = await client.post("/api/news/sources/x", json={"handle": "@OpenAI"}, headers=HEADERS)
    assert r.status_code == 201
    assert (r.json()["name"], r.json()["url"]) == ("@OpenAI", "https://x.com/openai")
    r = await client.post("/api/news/sources/x", json={"handle": "openai"}, headers=HEADERS)
    assert r.status_code == 409
    r = await client.post("/api/news/sources/x", json={"handle": "nope nope"}, headers=HEADERS)
    assert r.status_code == 422
    r = await client.post("/api/news/sources/x", json={"handle": "@A"})
    assert r.status_code == 403  # writes need the client header

    # The ordinary feed form understands profile addresses too, and rejects other x.com pages.
    feed = {"name": "x", "url": "https://twitter.com/AnthropicAI"}
    r = await client.post("/api/news/sources", json=feed, headers=HEADERS)
    assert r.status_code == 201 and r.json()["url"] == "https://x.com/anthropicai"
    feed["url"] = "https://x.com/a/status/1"
    assert (await client.post("/api/news/sources", json=feed, headers=HEADERS)).status_code == 422


async def test_the_x_filter_lists_every_account_and_nothing_else(client):  # noqa: F811
    await client.post("/api/news/refresh", headers=HEADERS)
    await wait_for_idle(client)
    everything = (await client.get("/api/news/articles")).json()
    assert everything["total"]  # the feeds have stories

    # Following nobody on X means nothing to show, not every story.
    only_x = {"x_only": "true"}
    assert (await client.get("/api/news/articles", params=only_x)).json()["total"] == 0
    counts = (await client.get("/api/news/categories", params=only_x)).json()
    assert not any(c["count"] for c in counts)  # category counts follow the same filter
    counts = (await client.get("/api/news/categories")).json()
    assert any(c["count"] for c in counts)

    async with client._transport.app.state.news.sessions() as s:
        accounts = [
            Source(name="@a", url="https://x.com/a"),
            Source(name="@b", url="https://twitter.com/b"),  # older address, still X
        ]
        s.add_all(accounts)
        await s.flush()
        for src in accounts:
            s.add(
                Article(
                    source_id=src.id,
                    url=f"{src.url}/status/1",
                    canonical_url=f"{src.url}/status/1",
                    title=f"Post from {src.name}",
                    title_hash=src.name,
                    status=ArticleStatus.SKIPPED.value,  # the AI's call doesn't hide X posts here
                )
            )
        await s.commit()
        one = accounts[0].id

    page = (await client.get("/api/news/articles", params=only_x)).json()
    assert sorted(a["source_name"] for a in page["items"]) == ["@a", "@b"]
    assert page["total"] == 2

    # One account narrows it, and the ordinary feed still leaves "not relevant" posts out.
    page = (await client.get("/api/news/articles", params={**only_x, "source_id": one})).json()
    assert [a["source_name"] for a in page["items"]] == ["@a"]
    feed = (await client.get("/api/news/articles")).json()
    assert feed["total"] == everything["total"]
