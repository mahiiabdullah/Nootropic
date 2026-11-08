"""X (Twitter) accounts as news sources, free: no key, no login, no payment.

X's own API bills per post, so two free tools do the reading:
- x-tweet-fetcher (`xtf`, MIT, pinned in pyproject.toml) reads single posts, rebuilding the
  full text of X Articles, which timelines leave empty. It also reads timelines through a
  Nitter server if you run one (`x_nitter_url`). Its browser backend is never used: it gets
  past public Nitter servers' bot blocks by posing as an ordinary browser.
- FxTwitter's public API (`/2/profile/{handle}/statuses`, docs.fxembed.com, checked
  2026-11-04) finds new posts when there's no Nitter, since `xtf` has no timeline without one.

Both are third-party with no uptime promise: if reading fails, the source's row says so and
the rest of the news keeps working. Only public accounts can be read.

Only an account's own posts are kept (no replies or reposts). Each new post costs one
summary from the AI provider, so at most `x_max_posts_per_check` new posts are taken each time.

How often an account is checked follows how recently it posted: every `x_refresh_minutes` if
it posted in the last day, down to every 12x that if it's been quiet for a week. Its first
new post puts it straight back on the fast schedule. The accounts that are due are spread
across news runs (`pick_due`), so no single run waits on all of them.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from xtf import Router as XtfRouter
from xtf import XtfError

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.core.logging import get_logger
from neurovex.modules.news.fetcher import FeedEntry, FeedResult
from neurovex.modules.news.models import Source

log = get_logger(__name__)

API = "https://api.fxtwitter.com/2"
MAX_TITLE = 120
MIN_GAP_S = 1.0  # a shared free service: one account at a time, a moment apart
TIMELINE_POSTS = 20
FULL_POST_TIMEOUT_S = 20.0
TWITTER_EPOCH_MS = 1288834974657  # post ids carry their creation time

_HANDLE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
_PROFILE_URL = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_NOT_ACCOUNTS = {
    "home",
    "search",
    "explore",
    "notifications",
    "messages",
    "settings",
    "i",
    "intent",
}
_LINK = re.compile(r"https?://\S+")
# (posted within, check every N x `x_refresh_minutes`); quieter than all of these: QUIET_FACTOR
ACTIVITY_TIERS = ((timedelta(days=1), 1), (timedelta(days=3), 2), (timedelta(days=7), 6))
QUIET_FACTOR = 12


class XError(Exception):
    """A failed read. The message is safe to show on the source's row."""


def parse_handle(raw: str) -> str | None:
    """ "@OpenAI", "OpenAI" or a profile URL → "OpenAI". None if it isn't an account."""
    text = (raw or "").strip()
    match = _PROFILE_URL.match(text) or _HANDLE.match(text)
    if match is None or match.group(1).lower() in _NOT_ACCOUNTS:
        return None
    return match.group(1)


def source_url(handle: str) -> str:
    return f"https://x.com/{handle.lower()}"


def is_x_source(url: str) -> bool:
    return bool(_PROFILE_URL.match(url or ""))


def is_x_url(url: str) -> bool:
    """Any x.com page: these need a login, so fetching the page for more text is pointless."""
    return urlsplit(url or "").hostname in ("x.com", "www.x.com", "twitter.com", "www.twitter.com")


def is_own_post(post: dict[str, Any]) -> bool:
    """The account's own words: not a reply (threads included), not a repost, not empty.
    An X Article counts even with no text: its words come from `merge_full_post`."""
    if post.get("replying_to") or post.get("reposted_by"):
        return False
    return bool(post_text(post) or post.get("article"))


def needs_full_post(post: dict[str, Any]) -> bool:
    """Timelines already carry whole posts, including long ones; only X Articles lack text."""
    return bool(post.get("article")) or "/i/article" in post_text(post)


def merge_full_post(post: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    """Add what xtf's single-post reader found to a timeline post."""
    article = full.get("article") or {}
    text = article.get("full_text") or article.get("preview_text") or full.get("text")
    merged = dict(post)
    if text:
        merged["text"] = text
    if title := article.get("title"):
        merged["article_title"] = title
    return merged


def from_xtf_tweet(tweet: Any, handle: str) -> dict[str, Any]:
    """An xtf Tweet (from Nitter) in the same shape as a FxTwitter post."""
    author = (tweet.author or "").lstrip("@")
    other_author = bool(author) and author.lower() != handle.lower()
    post: dict[str, Any] = {"id": tweet.tweet_id, "text": tweet.text}
    if tweet.retweeted_by or other_author:
        post["reposted_by"] = tweet.retweeted_by or author
    if post_text(post).startswith("@"):  # Nitter search doesn't mark replies; they lead with one
        post["replying_to"] = {"screen_name": handle}
    if quoted := tweet.quoted_tweet:
        post["quote"] = {"text": quoted.text, "author": {"screen_name": quoted.author.lstrip("@")}}
    return post


def post_text(post: dict[str, Any]) -> str:
    return (post.get("text") or "").strip()


def post_to_entry(post: dict[str, Any], handle: str) -> FeedEntry:
    text = post_text(post)
    first = next((line for line in text.splitlines() if _LINK.sub("", line).strip()), "")
    title = post.get("article_title") or " ".join(_LINK.sub("", first).split())
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1].rstrip() + "…"
    quote = post.get("quote") or {}
    if quoted := post_text(quote):  # "Interesting" means little without what it's quoting
        who = (quote.get("author") or {}).get("screen_name") or "someone"
        text += f"\n\nQuoting @{who}: {quoted}"
    stamp = post.get("created_timestamp")
    return FeedEntry(
        url=f"https://x.com/{handle}/status/{post['id']}",
        title=title or f"Post by @{handle}",
        author=f"@{handle}",
        published_at=datetime.fromtimestamp(stamp, UTC) if stamp else post_time(post["id"]),
        excerpt=text or None,
    )


def post_time(post_id: str | int) -> datetime:
    """When a post was made: X post ids carry their creation time."""
    return datetime.fromtimestamp(((int(post_id) >> 22) + TWITTER_EPOCH_MS) / 1000, UTC)


def check_every(src: Source, settings: Settings, now: datetime) -> timedelta:
    """How long to wait between checks of this account, from how recently it posted."""
    base = timedelta(minutes=settings.x_refresh_minutes)
    if not src.x_since_id:  # no post of its own seen yet
        return base * QUIET_FACTOR
    quiet_for = now - post_time(src.x_since_id)
    for within, factor in ACTIVITY_TIERS:
        if quiet_for < within:
            return base * factor
    return base * QUIET_FACTOR


def is_due(src: Source, settings: Settings, now: datetime) -> bool:
    return src.x_checked_at is None or now - src.x_checked_at >= check_every(src, settings, now)


def checks_per_run(settings: Settings, accounts: int) -> int:
    """Enough checks per news run to get through every account once per `x_refresh_minutes`."""
    runs = max(settings.x_refresh_minutes / settings.news_refresh_minutes, 1)
    return max(math.ceil(accounts / runs), 1)


def pick_due(sources: list[Source], settings: Settings, now: datetime) -> list[Source]:
    """The accounts to check this run: due ones, longest-waiting first, at most one run's share."""
    due = [s for s in sources if is_due(s, settings, now)]
    oldest = datetime.min.replace(tzinfo=UTC)
    due.sort(key=lambda s: s.x_checked_at or oldest)
    return due[: checks_per_run(settings, len(sources))]


def new_posts(
    posts: list[dict[str, Any]], since_id: str | None, limit: int
) -> list[dict[str, Any]]:
    """Own posts newer than `since_id`, newest first, at most `limit`.

    Sorted here because the service's order isn't strictly by date.
    """
    since = int(since_id) if since_id else 0
    fresh = [p for p in posts if int(p["id"]) > since and is_own_post(p)]
    return sorted(fresh, key=lambda p: int(p["id"]), reverse=True)[:limit]


def _error(handle: str, resp: httpx.Response) -> XError:
    if resp.status_code == 404:
        return XError(f"No public X account named @{handle}")
    if resp.status_code == 429:
        return XError("The free X service is rate limiting us: will try again later")
    return XError(f"The free X service returned HTTP {resp.status_code}")


class XReader:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.http = http
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        nitter = [settings.x_nitter_url] if settings.x_nitter_url else None
        self.xtf = XtfRouter(backend="nitter", nitter_instances=nitter)  # never its browser

    async def fetch(self, src: Source) -> FeedResult:
        handle = parse_handle(src.url)
        if handle is None:
            raise XError(f"Not an X account: {src.url}")
        now = utcnow()
        if not is_due(src, self.settings, now):
            return self.not_due(src)

        async with self._lock:
            if (wait := self._last_call + MIN_GAP_S - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            results = await self._timeline(handle)
            try:
                posts = new_posts(results, src.x_since_id, self.settings.x_max_posts_per_check)
            except (ValueError, TypeError, KeyError) as e:
                raise XError("The free X service sent something unexpected") from e
            posts = [await self._with_full_text(p, handle) for p in posts]

        async with self.sessions() as s:
            row = await s.get(Source, src.id)
            if row is not None:
                row.x_checked_at = now
                if posts:
                    row.x_since_id = posts[0]["id"]
                await s.commit()
        (log.info if posts else log.debug)("news_x_checked", account=handle, new=len(posts))
        return FeedResult(200, [post_to_entry(p, handle) for p in posts], None, None)

    @staticmethod
    def not_due(src: Source) -> FeedResult:
        """Not checked this time: nothing about the source changes."""
        return FeedResult(304, [], None, None, src.last_error)

    async def _timeline(self, handle: str) -> list[dict[str, Any]]:
        """Recent posts: through your own Nitter if you set one, otherwise FxTwitter."""
        if self.settings.x_nitter_url:
            try:
                tweets = await asyncio.to_thread(
                    self.xtf.fetch_timeline, handle, limit=TIMELINE_POSTS
                )
                return [from_xtf_tweet(t, handle) for t in tweets if t.tweet_id]
            except XtfError as e:
                log.warning("news_x_nitter_failed", account=handle, code=e.code)
        try:
            resp = await self.http.get(f"{API}/profile/{handle}/statuses")
        except httpx.HTTPError as e:
            raise XError(f"Could not reach the free X service: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise _error(handle, resp)
        try:
            return resp.json().get("results") or []
        except (ValueError, AttributeError) as e:
            raise XError("The free X service sent something unexpected") from e

    async def _with_full_text(self, post: dict[str, Any], handle: str) -> dict[str, Any]:
        if not needs_full_post(post):
            return post
        try:
            full = await asyncio.wait_for(
                asyncio.to_thread(self.xtf.fetch_tweet, handle, post["id"]), FULL_POST_TIMEOUT_S
            )
        except Exception as e:  # a third-party parser failing must not lose the post
            log.warning("news_x_full_post_failed", account=handle, error=type(e).__name__)
            return post
        return merge_full_post(post, full)
