"""Fetching feeds and article pages over HTTP.

Feeds use conditional GET (ETag / Last-Modified) so unchanged feeds cost one
cheap 304. Article pages are fetched only when the feed excerpt is too thin to
summarize, then reduced to readable text with trafilatura.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx

from neurovex import __version__

USER_AGENT = f"Neurovex/{__version__} (personal news reader; +https://localhost)"
MAX_PAGE_BYTES = 3_000_000


@dataclass(frozen=True)
class FeedEntry:
    url: str
    title: str
    author: str | None
    published_at: datetime | None
    excerpt: str | None


@dataclass(frozen=True)
class FeedResult:
    status_code: int
    entries: list[FeedEntry]
    etag: str | None
    last_modified: str | None
    note: str | None = None  # why nothing was fetched, shown on the source's row

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


def build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
    )


def html_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _entry_datetime(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:  # feedparser normalizes to a UTC struct_time
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
            except (OverflowError, ValueError):
                return None
    return None


# Link aggregators (hnrss) put only metadata in the description:
# "Article URL: … Comments URL: … Points: 15 # Comments: 9". That is not article text.
_AGGREGATOR_META = re.compile(r"^Article URL:\s*\S+\s+Comments URL:", re.IGNORECASE)


def _entry_excerpt(entry: feedparser.FeedParserDict) -> str | None:
    # Prefer full content (some feeds ship whole articles), then the summary.
    contents = entry.get("content") or []
    best = max((c.get("value", "") for c in contents), key=len, default="")
    text = html_to_text(best) or html_to_text(entry.get("summary"))
    if text and _AGGREGATOR_META.match(text):
        return None
    return text


def parse_feed(content: bytes) -> list[FeedEntry]:
    parsed = feedparser.parse(content)
    entries: list[FeedEntry] = []
    for e in parsed.entries:
        link = e.get("link")
        title = html_to_text(e.get("title"))
        if not link or not title:
            continue
        entries.append(
            FeedEntry(
                url=link,
                title=title[:500],
                author=(e.get("author") or None),
                published_at=_entry_datetime(e),
                excerpt=_entry_excerpt(e),
            )
        )
    if not entries and parsed.bozo:
        raise ValueError(f"Could not parse feed: {parsed.get('bozo_exception')}")
    return entries


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FeedResult:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    resp = await client.get(url, headers=headers)
    if resp.status_code == 304:
        return FeedResult(304, [], etag, last_modified)
    resp.raise_for_status()
    # feedparser is synchronous and CPU-bound; keep it off the event loop.
    entries = await asyncio.to_thread(parse_feed, resp.content)
    return FeedResult(
        resp.status_code,
        entries,
        resp.headers.get("ETag"),
        resp.headers.get("Last-Modified"),
    )


async def fetch_article_text(client: httpx.AsyncClient, url: str) -> str | None:
    """Download a page and extract its main text. Returns None on any failure."""
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    break
                chunks.append(chunk)
            body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError:
        return None

    import trafilatura

    text = await asyncio.to_thread(
        trafilatura.extract, body, include_comments=False, include_tables=False
    )
    return text.strip() if text else None
