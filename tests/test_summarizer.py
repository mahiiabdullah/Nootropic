from __future__ import annotations

import json

import httpx
import pytest

from neurovex.llm.catalog import PROVIDERS
from neurovex.llm.errors import InvalidOutput, RateLimited
from neurovex.llm.ratelimit import RateGate
from neurovex.llm.router import LLMRouter
from neurovex.modules.news.summarizer import (
    SUMMARY_SCHEMA,
    ArticleInput,
    ArticleSummary,
    ProvidersUnavailable,
    RouterSummarizer,
    SummarizeError,
    build_user_message,
    router_summarizer_factory,
)
from tests.test_llm import fake_builder

GOOD = {
    "relevant": True,
    "title": "Clean title",
    "summary": "S.",
    "what_happened": "W.",
    "why_it_matters": "M.",
    "key_points": ["a", " ", "b"],
    "categories": ["AI", "AI", "Cybersecurity"],
    "importance": 14,
}

ARTICLE = ArticleInput("Src", "Title", "https://e.com/x", None, "x" * 20_000)


def test_summary_validation_normalizes():
    s = ArticleSummary.model_validate(GOOD)
    assert s.importance == 10
    assert [c.value for c in s.categories] == ["AI", "Cybersecurity"]
    assert s.key_points == ["a", "b"]


def test_schema_lists_every_field_as_required():
    assert set(SUMMARY_SCHEMA["required"]) == set(SUMMARY_SCHEMA["properties"])
    assert SUMMARY_SCHEMA["additionalProperties"] is False


def test_user_message_fences_and_truncates():
    msg = build_user_message(ARTICLE, max_chars=1000)
    assert "<article>" in msg and "</article>" in msg
    body = msg.split("<article>\n")[1].split("\n</article>")[0]
    assert body == "x" * 1000


def _story(i: int):
    from neurovex.modules.news.summarizer import BriefingStory

    return BriefingStory(i, f"Story {i}", "Src", "S", f"Why {i}", 5, [])


def test_complete_briefing_filters_and_tops_up():
    from neurovex.modules.news.summarizer import (
        BriefingDraft,
        BriefingItem,
        BriefingRequest,
        complete_briefing,
    )

    req = BriefingRequest("now", [], [], [_story(i) for i in range(1, 7)])
    draft = BriefingDraft(
        headline="H",
        overview="O",
        items=[
            BriefingItem(article_id=3, take="Mine"),
            BriefingItem(article_id=3, take="dup"),
            BriefingItem(article_id=99, take="invented"),
        ],
        watch="W",
    )
    out = complete_briefing(draft, req)
    assert [(i.article_id, i.take) for i in out.items] == [
        (3, "Mine"), (1, "Why 1"), (2, "Why 2"), (4, "Why 4"),
    ]  # fmt: skip

    with pytest.raises(SummarizeError):
        complete_briefing(BriefingDraft(headline="H", overview="O", items=[], watch=""), req)


async def make_router(settings, scripts):
    settings = settings.model_copy(update={"llm_providers": ",".join(scripts)})
    http = httpx.AsyncClient()
    return LLMRouter(settings, http, builder=fake_builder(scripts)), http


async def test_router_summarizer_fails_over_and_labels_the_model(settings):
    router, http = await make_router(
        settings, {"groq": [RateLimited("groq", "slow down", 30)], "cerebras": [json.dumps(GOOD)]}
    )
    summary = await RouterSummarizer(router).summarize(ARTICLE)
    await http.aclose()
    assert summary.title == "Clean title" and summary.model == "cerebras/fake-cerebras"
    assert router.states["groq"].cooldown_until is not None


async def test_transient_failures_raise_providers_unavailable(settings):
    # Retry-after longer than the background wait budget: give up now, don't wait.
    router, http = await make_router(settings, {"groq": [RateLimited("groq", "x", 600)]})
    with pytest.raises(ProvidersUnavailable):
        await RouterSummarizer(router).summarize(ARTICLE)
    with pytest.raises(ProvidersUnavailable):  # now cooling down: nothing to try
        await RouterSummarizer(router).summarize(ARTICLE)
    await http.aclose()


async def test_invalid_output_everywhere_is_the_articles_problem(settings):
    router, http = await make_router(
        settings, {"groq": ['{"relevant": true}'], "cerebras": [InvalidOutput("cerebras", "junk")]}
    )
    for st in router.states.values():  # each provider gets a second try; pacing isn't tested here
        st.gate = RateGate()
    with pytest.raises(SummarizeError) as exc:
        await RouterSummarizer(router).summarize(ARTICLE)
    await http.aclose()
    assert not isinstance(exc.value, ProvidersUnavailable)


async def test_factory_honours_news_llm_provider(settings):
    router, http = await make_router(settings, {"groq": [], "cerebras": []})
    factory = router_summarizer_factory(router)
    auto = await factory(settings.model_copy(update={"news_llm_provider": "auto"}), http)
    pinned = await factory(settings.model_copy(update={"news_llm_provider": "cerebras"}), http)
    none = await factory(settings.model_copy(update={"news_llm_provider": "none"}), http)
    missing = await factory(settings.model_copy(update={"news_llm_provider": "mistral"}), http)
    await http.aclose()
    assert auto.name.startswith("groq/") and "+1 fallback" in auto.name
    assert pinned.name == "cerebras/fake-cerebras" and pinned.only == "cerebras"
    assert none is None and missing is None
    assert set(PROVIDERS) >= {"groq", "cerebras"}


def test_off_list_categories_are_dropped_not_fatal():
    s = ArticleSummary.model_validate({**GOOD, "categories": ["Space", "cybersecurity", "AI"]})
    assert [c.value for c in s.categories] == ["Cybersecurity", "AI"]
    s = ArticleSummary.model_validate({**GOOD, "categories": ["Space"]})
    assert [c.value for c in s.categories] == ["Technology"]
