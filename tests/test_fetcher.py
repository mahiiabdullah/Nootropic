from __future__ import annotations

import httpx
import pytest
import respx

from neurovex.modules.news import fetcher
from tests.conftest import hours_ago, rss

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom</title>
  <entry>
    <title>Atom &amp; entry</title>
    <link href="https://example.org/a"/>
    <updated>2026-10-08T10:00:00Z</updated>
    <author><name>Ada</name></author>
    <content type="html">&lt;p&gt;Full &lt;b&gt;content&lt;/b&gt; here&lt;/p&gt;&lt;script&gt;x()&lt;/script&gt;</content>
  </entry>
</feed>"""


def test_parse_rss():
    entries = fetcher.parse_feed(
        rss(("Hello", "https://e.com/1", hours_ago(1)), ("Undated", "https://e.com/2", None))
    )
    assert [e.title for e in entries] == ["Hello", "Undated"]
    assert entries[0].published_at is not None and entries[0].published_at.tzinfo is not None
    assert entries[0].excerpt == "Body of Hello."
    assert entries[1].published_at is None


def test_parse_atom_prefers_content_and_strips_html():
    [e] = fetcher.parse_feed(ATOM)
    assert e.title == "Atom & entry"
    assert e.author == "Ada"
    assert e.excerpt == "Full content here"
    assert e.published_at is not None and e.published_at.hour == 10  # UTC preserved


def test_aggregator_metadata_is_not_treated_as_article_text():
    meta = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>HN</title><item>'
        b"<title>Show HN: Thing</title><link>https://thing.test/</link>"
        b"<description>&lt;p&gt;Article URL: &lt;a href=&quot;https://thing.test/&quot;&gt;"
        b"https://thing.test/&lt;/a&gt;&lt;/p&gt;&lt;p&gt;Comments URL: x&lt;/p&gt;"
        b"&lt;p&gt;Points: 15&lt;/p&gt;</description></item></channel></rss>"
    )
    [e] = fetcher.parse_feed(meta)
    assert e.excerpt is None


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        fetcher.parse_feed(b"<html>not a feed")


@respx.mock
async def test_fetch_feed_conditional_get():
    route = respx.get("https://feeds.test/rss")
    route.side_effect = [
        httpx.Response(
            200, content=rss(("A", "https://e.com/a", hours_ago(1))), headers={"ETag": '"v1"'}
        ),
        httpx.Response(304),
    ]
    async with fetcher.build_http_client() as client:
        first = await fetcher.fetch_feed(client, "https://feeds.test/rss")
        second = await fetcher.fetch_feed(client, "https://feeds.test/rss", etag=first.etag)
    assert first.etag == '"v1"' and len(first.entries) == 1
    assert second.not_modified and second.entries == []
    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'


@respx.mock
async def test_fetch_feed_http_error_raises():
    respx.get("https://feeds.test/broken").respond(500)
    async with fetcher.build_http_client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.fetch_feed(client, "https://feeds.test/broken")


@respx.mock
async def test_fetch_article_text_rejects_non_html():
    respx.get("https://e.com/file.pdf").respond(
        200, content=b"%PDF", headers={"Content-Type": "application/pdf"}
    )
    async with fetcher.build_http_client() as client:
        assert await fetcher.fetch_article_text(client, "https://e.com/file.pdf") is None
