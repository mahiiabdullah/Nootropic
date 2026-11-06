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
    TWITTER_EPOCH_MS,
    from_xtf_tweet,
    is_own_post,
    is_x_source,
    is_x_url,
    merge_full_post,
    needs_full_post,
    new_posts,
    parse_handle,
    post_to_entry,
    source_url,
)
from tests.conftest import FakeSummarizer
from tests.test_api import HEADERS, client, wait_for_idle  # noqa: F401  (fixture)

STATUSES = f"{API}/profile/OpenAI/statuses"
RECENT_ID = str((int(utcnow().timestamp() * 1000) - TWITTER_EPOCH_MS) << 22)  # a post from now


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

    async with sessions() as s:
        row = await s.get(Source, src.id)
        row.x_checked_at = utcnow() - timedelta(minutes=31)
        await s.commit()
    run = await news.pipeline.run()
    assert route.call_count == 2 and run.articles_new == 1
    assert (await _source(sessions)).x_since_id == "102"


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
