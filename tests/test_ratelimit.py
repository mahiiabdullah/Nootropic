from __future__ import annotations

import time

import httpx
import pytest
import respx

from neurovex.llm.catalog import PROVIDERS
from neurovex.llm.errors import ProvidersExhausted, RateLimited
from neurovex.llm.providers import OpenAICompatProvider
from neurovex.llm.ratelimit import (
    RateGate,
    RateInfo,
    duration_s,
    estimate_tokens,
    parse_rate_headers,
)
from tests.test_llm import OK, REQ, completion, parse_ok, router_for

GROQ_HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-requests": "998",
    "x-ratelimit-remaining-tokens": "5936",
    "x-ratelimit-reset-requests": "2m52.8s",
    "x-ratelimit-reset-tokens": "15.48s",
}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_duration_parsing():
    assert duration_s("7.66s") == 7.66
    assert duration_s("2m59.5s") == pytest.approx(179.5)
    assert duration_s("120") == 120
    assert duration_s(None) is None
    assert duration_s(str(int((time.time() + 30) * 1000))) == pytest.approx(30, abs=2)  # epoch ms


def test_parse_groq_headers():
    info = parse_rate_headers(httpx.Headers(GROQ_HEADERS))
    assert info == RateInfo(
        remaining_requests=998, reset_requests_s=pytest.approx(172.8), remaining_tokens=5936,
        reset_tokens_s=pytest.approx(15.48), limit_tokens=8000,
    )  # fmt: skip
    assert parse_rate_headers(httpx.Headers({"content-type": "application/json"})) is None


def test_estimate_tokens():
    assert estimate_tokens("s" * 3600, "u" * 3600, 4000) == 2000 + 4000  # Groq charges max_tokens
    assert estimate_tokens("", "", 100) == 100


def test_gate_requests_per_minute():
    clock = Clock()
    gate = RateGate(rpm=2, clock=clock)
    gate.reserve(10)
    clock.t += 10
    gate.reserve(10)
    assert gate.wait_s(10) == pytest.approx(50)  # first request leaves the window in 50 s
    clock.t += 50
    assert gate.wait_s(10) == 0


def test_gate_tokens_per_minute_and_settle():
    clock = Clock()
    gate = RateGate(tpm=8000, clock=clock)
    h1 = gate.reserve(3000)
    clock.t += 20
    gate.reserve(3000)
    assert gate.wait_s(3000) == pytest.approx(40)  # needs the first one to expire
    gate.settle(h1, 1500)  # it actually used fewer tokens
    assert gate.wait_s(3000) == 0
    assert gate.wait_s(20000) == pytest.approx(
        60
    )  # bigger than the budget: wait for a clean window


def test_gate_learns_from_headers():
    clock = Clock()
    gate = RateGate(tpm=8000, clock=clock)
    gate.observe(RateInfo(remaining_tokens=1000, reset_tokens_s=12, limit_tokens=6000))
    assert gate.tpm == 6000
    assert gate.wait_s(2000) == pytest.approx(12)
    assert gate.wait_s(500) == 0
    clock.t += 12
    assert gate.wait_s(2000) == 0
    gate.observe(RateInfo(remaining_requests=0, reset_requests_s=300))
    assert gate.wait_s(1) == pytest.approx(300)  # daily requests used up


@respx.mock
async def test_provider_result_carries_rate_info():
    respx.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200, json=completion(OK), headers=GROQ_HEADERS
    )
    async with httpx.AsyncClient() as http:
        result = await OpenAICompatProvider(PROVIDERS["groq"], "m", "k", http).complete_json(REQ)
    assert result.rate is not None and result.rate.remaining_tokens == 5936


async def test_router_waits_for_rate_budget_within_max_wait(settings):
    r = router_for(settings, {"groq": [OK]})
    r.states["groq"].gate.observe(RateInfo(remaining_tokens=0, reset_tokens_s=0.2))
    with pytest.raises(ProvidersExhausted) as exc:  # no waiting allowed
        await r.complete_json(REQ, parse_ok)
    assert exc.value.kinds == [] and 0 < exc.value.retry_in_s <= 0.21
    started = time.perf_counter()
    _, result = await r.complete_json(REQ, parse_ok, max_wait_s=2)
    assert result.provider == "groq" and time.perf_counter() - started >= 0.15


async def test_single_provider_rides_out_a_short_429(settings):
    """Groq-only setup: a 429 with a short Retry-After is waited out, not fatal."""
    r = router_for(settings, {"groq": [RateLimited("groq", "TPM", 0.2), OK]})
    _, result = await r.complete_json(REQ, parse_ok, max_wait_s=3)
    assert result.provider == "groq" and result.attempts == ["groq: rate_limited"]


async def test_long_429_is_not_waited_for(settings):
    r = router_for(settings, {"groq": [RateLimited("groq", "TPD", 900)]})
    with pytest.raises(ProvidersExhausted) as exc:
        await r.complete_json(REQ, parse_ok, max_wait_s=5)
    assert exc.value.kinds == ["rate_limited"] and exc.value.retry_in_s > 800


def test_status_shows_rate_state(settings):
    r = router_for(settings, {"groq": [OK]})
    rate = r.status()[0]["rate"]
    assert rate["rpm_limit"] == 30 and rate["tpm_limit"] == 8000 and rate["next_slot_s"] == 0


async def test_background_requests_leave_headroom_for_chat(settings):
    r = router_for(settings, {"groq": [OK, OK]})
    gate = r.states["groq"].gate
    gate.observe(RateInfo(remaining_tokens=2000, reset_tokens_s=30))
    with pytest.raises(ProvidersExhausted):  # background: would eat the reserve
        await r.complete_json(REQ, parse_ok, reserve_tokens=2500)
    _, result = await r.complete_json(REQ, parse_ok)  # interactive: fits in what's left
    assert result.provider == "groq"
