from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from neurovex.core.config import Profile, Settings
from neurovex.core.db import create_engine, create_sessionmaker, init_db
from neurovex.modules.news.summarizer import (
    ArticleInput,
    ArticleSummary,
    BriefingDraft,
    BriefingItem,
    BriefingRequest,
    Category,
    SummarizeError,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        profile=Profile.TEST,
        data_dir=tmp_path / "data",
        news_llm_provider="none",
        news_auto_refresh=False,
        news_fetch_full_text=False,
    )


@pytest.fixture
async def sessions(settings: Settings):
    settings.ensure_dirs()
    engine = create_engine(settings.db_url)
    await init_db(engine)
    yield create_sessionmaker(engine)
    await engine.dispose()


def rss(*items: tuple[str, str, datetime | None], title: str = "Test Feed") -> bytes:
    """Build a small RSS 2.0 document from (title, link, published) tuples."""
    parts = []
    for t, link, published in items:
        date = f"<pubDate>{format_datetime(published)}</pubDate>" if published else ""
        parts.append(
            f"<item><title>{t}</title><link>{link}</link>{date}"
            f"<description>&lt;p&gt;Body of {t}.&lt;/p&gt;</description></item>"
        )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>{title}</title><link>https://example.com</link>{''.join(parts)}"
        "</channel></rss>"
    ).encode()


def hours_ago(h: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=h)


KNOWN_TOPICS = ("Rust", "Python", "OpenAI", "Windows", "NetScaler", "Linux", "Crypto")


class FakeSummarizer:
    """Deterministic LLM stand-in.

    Titles containing 'deal' are off-topic, 'boom' fails, 'critical' scores 9.
    Topics are any of KNOWN_TOPICS that appear in the title.
    """

    is_local = False
    name = "fake-model"

    def __init__(self) -> None:
        self.calls: list[ArticleInput] = []
        self.briefings: list[BriefingRequest] = []

    async def summarize(self, article: ArticleInput) -> ArticleSummary:
        self.calls.append(article)
        title = article.title.lower()
        if "boom" in title:
            raise SummarizeError("model exploded")
        relevant = "deal" not in title
        return ArticleSummary(
            relevant=relevant,
            title=article.title,
            summary=f"Summary of {article.title}.",
            what_happened="Things happened.",
            why_it_matters="It matters.",
            key_points=["one", "two"],
            categories=[Category.AI if " ai " in f" {title} " else Category.TECHNOLOGY],
            topics=[t for t in KNOWN_TOPICS if t.lower() in title],
            importance=9 if "critical" in title else 7 if relevant else 5,
        )

    async def write_briefing(self, request: BriefingRequest) -> BriefingDraft:
        self.briefings.append(request)
        return BriefingDraft(
            headline="Your news, briefly",
            overview=f"{len(request.stories)} stories worth your time.",
            items=[
                BriefingItem(article_id=s.article_id, take=f"Take on {s.title}")
                for s in request.stories
            ],
            watch="Watch this space.",
        )


@pytest.fixture
def fake_summarizer() -> FakeSummarizer:
    return FakeSummarizer()


def signed_in(settings: Settings) -> dict[str, str]:
    """Headers for a test client: the API token, as a script would send it."""
    from neurovex.security.auth import load_api_token

    return {"Authorization": f"Bearer {load_api_token(settings)}"}
