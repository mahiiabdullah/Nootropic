"""LLM router: an ordered chain of providers with failover, cooldowns and load spreading.

For each request the router tries providers in order (or round-robin) and skips any
that are cooling down, disabled or already at their concurrency limit:

- 429 rate limit   → cool down for the provider's Retry-After (escalating if repeated)
- 402 / quota      → cool down for an hour
- 5xx / timeout    → short, escalating cooldown
- bad key (401/403)→ disabled until the key changes (`refresh()` re-reads keys)
- unknown model    → disabled until restart (it's a config problem)
- invalid JSON     → counts as a failure; the next provider gets the request

Every attempt is recorded (provider, model, task, outcome, latency, tokens) so you can
see which free tiers are doing the work: `nvx llm status`, GET /api/llm/providers.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from neurovex.core.db import utcnow
from neurovex.core.logging import get_logger
from neurovex.llm.catalog import PROVIDERS, ProviderSpec
from neurovex.llm.errors import (
    AuthError,
    InvalidOutput,
    LLMError,
    ModelNotFound,
    ProvidersExhausted,
    QuotaExhausted,
    RateLimited,
)
from neurovex.llm.providers import (
    AnthropicProvider,
    MessagesProvider,
    OllamaProvider,
    OpenAICompatProvider,
    Provider,
)
from neurovex.llm.ratelimit import RateGate, estimate_tokens, prompt_tokens
from neurovex.llm.types import JSONRequest, LLMResult

if TYPE_CHECKING:
    from neurovex.core.config import Settings

log = get_logger(__name__)

RATE_LIMIT_BASE_S = 30.0
QUOTA_COOLDOWN_S = 3600.0
UNAVAILABLE_BASE_S = 15.0
MAX_COOLDOWN_S = 3600.0


@dataclass
class CallRecord:
    provider: str
    model: str
    task: str
    outcome: (
        str  # ok | rate_limited | quota | auth | model | bad_request | unavailable | invalid_output
    )
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    at: datetime = field(default_factory=utcnow)


Recorder = Callable[[CallRecord], Awaitable[None]]


@dataclass
class ProviderState:
    spec: ProviderSpec
    provider: Provider | None = None  # None = not configured (no key)
    key_fingerprint: str | None = None
    disabled_reason: str | None = None
    cooldown_until: datetime | None = None
    consecutive_failures: int = 0
    consecutive_rate_limits: int = 0
    in_flight: int = 0
    max_concurrency: int = 2
    calls: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    total_latency_ms: int = 0
    last_error: str | None = None
    last_ok_at: datetime | None = None
    day: str = ""
    calls_today: int = 0
    gate: RateGate = field(default_factory=RateGate)

    @property
    def configured(self) -> bool:
        return self.provider is not None

    def available(self, now: datetime) -> bool:
        return (
            self.configured
            and self.disabled_reason is None
            and (self.cooldown_until is None or self.cooldown_until <= now)
        )

    def snapshot(self, now: datetime) -> dict[str, Any]:
        cooling = self.cooldown_until and self.cooldown_until > now
        if not self.configured:
            status = "not_configured"
        elif self.disabled_reason:
            status = "disabled"
        elif cooling:
            status = "cooling_down"
        else:
            status = "ready"
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "status": status,
            "model": self.provider.model if self.provider else None,
            "json_mode": getattr(self.provider, "json_mode", None),
            "configured": self.configured,
            "key_env": self.spec.key_env,
            "account_env": self.spec.account_env,
            "disabled_reason": self.disabled_reason,
            "cooldown_until": self.cooldown_until if cooling else None,
            "in_flight": self.in_flight,
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
            "calls_today": self.calls_today if self.day == now.date().isoformat() else 0,
            "avg_latency_ms": round(self.total_latency_ms / self.successes)
            if self.successes
            else None,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "rate": self.gate.snapshot(),
            "free_tier": self.spec.free_tier,
            "signup_url": self.spec.signup_url,
        }


ProviderBuilder = Callable[
    [ProviderSpec, "Settings", httpx.AsyncClient], tuple[Provider | None, str | None]
]


def build_provider(
    spec: ProviderSpec, settings: Settings, http: httpx.AsyncClient
) -> tuple[Provider | None, str | None]:
    """Create a provider client from settings, or (None, None) if its key is missing."""
    model = settings.model_for(spec.id) or spec.default_model
    if spec.kind == "ollama":
        return OllamaProvider(spec, model, settings.ollama_url, http), "local"
    key = settings.secret(spec.key_env, *spec.alt_key_envs)
    if not key:
        return None, None
    fingerprint = f"{len(key)}:{key[-4:]}"
    if spec.kind == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=key, max_retries=0, timeout=settings.llm_timeout_s
        )
        return AnthropicProvider(spec, model, client), fingerprint
    account = None
    if spec.account_env:
        account = settings.secret(spec.account_env)
        if not account:
            return None, None
        fingerprint += f"@{account[-4:]}"
    cls = MessagesProvider if spec.kind == "messages" else OpenAICompatProvider
    return cls(spec, model, key, http, account_id=account), fingerprint


class LLMRouter:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        recorder: Recorder | None = None,
        builder: ProviderBuilder = build_provider,
        owns_http: bool = False,
    ) -> None:
        self.settings = settings
        self.http = http
        self.recorder = recorder
        self._builder = builder
        self._owns_http = owns_http
        self.order: list[str] = settings.llm_provider_order()
        self.states: dict[str, ProviderState] = {
            pid: ProviderState(
                spec=PROVIDERS[pid],
                max_concurrency=settings.llm_concurrency_for(pid),
                gate=RateGate(rpm=PROVIDERS[pid].rpm, tpm=PROVIDERS[pid].tpm),
            )
            for pid in self.order
        }
        self._rr = itertools.count()
        self.refresh()

    @classmethod
    def from_settings(cls, settings: Settings, recorder: Recorder | None = None) -> LLMRouter:
        http = httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_s, connect=10.0))
        return cls(settings, http, recorder, owns_http=True)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    # --- configuration ----------------------------------------------------------

    def refresh(self) -> list[str]:
        """(Re)build provider clients from current keys. Returns configured provider ids."""
        for pid, st in self.states.items():
            try:
                provider, fingerprint = self._builder(st.spec, self.settings, self.http)
            except Exception as e:  # a broken optional SDK must not break the router
                log.warning("llm_provider_build_failed", provider=pid, error=str(e)[:200])
                provider, fingerprint = None, None
            key_changed = fingerprint != st.key_fingerprint
            if provider is None:
                st.provider, st.key_fingerprint = None, None
                continue
            if st.provider is None or key_changed or provider.model != st.provider.model:
                st.provider, st.key_fingerprint = provider, fingerprint
                if st.disabled_reason in ("auth", "model") and key_changed:
                    st.disabled_reason = None  # new key: give it another chance
        return self.configured()

    def configured(self) -> list[str]:
        return [pid for pid in self.order if self.states[pid].configured]

    def has_providers(self) -> bool:
        return bool(self.configured())

    @property
    def all_local(self) -> bool:
        ids = self.configured()
        return bool(ids) and all(self.states[p].provider.is_local for p in ids)  # type: ignore[union-attr]

    def label(self) -> str:
        ids = [p for p in self.configured() if self.states[p].disabled_reason is None]
        if not ids:
            return "none"
        first = self.states[ids[0]].provider
        extra = f" +{len(ids) - 1} fallback{'s' if len(ids) > 2 else ''}" if len(ids) > 1 else ""
        return f"{ids[0]}/{first.model}{extra}"  # type: ignore[union-attr]

    def status(self) -> list[dict[str, Any]]:
        now = utcnow()
        return [self.states[pid].snapshot(now) for pid in self.order]

    # --- selection --------------------------------------------------------------

    def _candidates(self, now: datetime, only: str | None = None) -> list[ProviderState]:
        ids = [only] if only else list(self.order)
        if not only and self.settings.llm_strategy == "round_robin" and ids:
            start = next(self._rr) % len(ids)
            ids = ids[start:] + ids[:start]
        ready = [self.states[p] for p in ids if p in self.states and self.states[p].available(now)]
        # Prefer providers with free concurrency slots (overflow to the next one under load),
        # keeping the configured order within each group.
        free = [s for s in ready if s.in_flight < s.max_concurrency]
        busy = sorted(
            (s for s in ready if s.in_flight >= s.max_concurrency), key=lambda s: s.in_flight
        )
        return free + busy

    # --- calls -------------------------------------------------------------------

    async def complete_json[T](
        self,
        req: JSONRequest,
        parse: Callable[[str], T],
        only: str | None = None,
        max_wait_s: float = 0.0,
        reserve_tokens: int = 0,
    ) -> tuple[T, LLMResult]:
        """Run `req` on the first provider that returns output `parse` accepts.

        Providers are used in order, skipping any that are cooling down, disabled or
        out of rate budget right now. If none has capacity, wait up to `max_wait_s`
        for the soonest one (background jobs pass ~90 s; interactive use a little).
        `parse` should raise on invalid output (e.g. pydantic ValidationError); that
        counts as a failed attempt and the next provider is tried.

        Background callers pass `reserve_tokens` to leave that much of each provider's
        per-minute budget free, so interactive requests (chat) aren't starved.
        """
        est = estimate_tokens(req.system, req.user, req.max_tokens)
        need = est + reserve_tokens
        deadline = time.monotonic() + max_wait_s
        attempts: list[str] = []
        kinds: list[str] = []
        failures: dict[str, int] = {}
        excluded: set[str] = set()
        while True:
            now = utcnow()
            candidates = [s for s in self._candidates(now, only) if s.spec.id not in excluded]
            ready = [s for s in candidates if s.gate.wait_s(need) <= 0]
            if ready:
                st = ready[0]
                outcome = await self._attempt(st, req, parse, est)
                if isinstance(outcome, LLMError):
                    attempts.append(f"{st.spec.id}: {outcome.kind}")
                    kinds.append(outcome.kind)
                    failures[st.spec.id] = failures.get(st.spec.id, 0) + 1
                    # Rate limits/outages may clear within the wait budget, and bad output is
                    # often a one-off, so those get more tries; a bad key or bad request won't
                    # change for this request.
                    tries = {"rate_limited": 3, "unavailable": 3, "invalid_output": 2}
                    if failures[st.spec.id] >= tries.get(outcome.kind, 1):
                        excluded.add(st.spec.id)
                    continue
                value, result = outcome
                result.attempts = attempts
                return value, result
            # Nothing has capacity right now: wait for the soonest slot if the budget allows.
            waits = [s.gate.wait_s(need) for s in candidates]
            waits += self._cooldown_waits(now, only, excluded)
            if not waits:
                break
            wait = min(waits)
            if time.monotonic() + wait > deadline:
                break
            await asyncio.sleep(wait + 0.05)

        now = utcnow()
        retry_in = min(
            [s.gate.wait_s(need) for s in self._candidates(now, only)]
            + self._cooldown_waits(now, only, set()),
            default=None,
        )
        if not attempts:
            raise ProvidersExhausted(self._exhausted_message(only), retry_in, [])
        raise ProvidersExhausted("All providers failed: " + "; ".join(attempts), retry_in, kinds)

    async def _attempt[T](
        self, st: ProviderState, req: JSONRequest, parse: Callable[[str], T], est: int
    ) -> tuple[T, LLMResult] | LLMError:
        provider = st.provider
        assert provider is not None
        st.in_flight += 1
        handle = st.gate.reserve(est)
        settled = False
        try:
            result = await provider.complete_json(req)
            st.gate.observe(result.rate)
            if result.input_tokens is not None:
                st.gate.settle(handle, (result.input_tokens or 0) + (result.output_tokens or 0))
                settled = True
            try:
                value = parse(result.text)
            except Exception as e:
                raise InvalidOutput(st.spec.id, f"failed validation: {str(e)[:200]}") from e
        except LLMError as e:
            if not settled:
                # Refused up front (limits, keys, model): nothing was used. Otherwise assume
                # the prompt was read but no answer came back: drop the output reserve.
                refused = e.kind in ("rate_limited", "quota", "auth", "model")
                st.gate.settle(handle, 0 if refused else prompt_tokens(req.system, req.user))
            self._on_failure(st, e)
            await self._record(st, req, e.kind, 0, error=e.message)
            log.warning(
                "llm_attempt_failed", provider=st.spec.id, kind=e.kind, error=e.message[:200]
            )
            return e
        finally:
            st.in_flight -= 1
        self._on_success(st, result)
        await self._record(st, req, "ok", result.latency_ms, result=result)
        return value, result

    def _cooldown_waits(self, now: datetime, only: str | None, excluded: set[str]) -> list[float]:
        ids = [only] if only else self.order
        return [
            (st.cooldown_until - now).total_seconds()
            for pid in ids
            if pid not in excluded
            and (st := self.states.get(pid)) is not None
            and st.configured
            and st.disabled_reason is None
            and st.cooldown_until is not None
            and st.cooldown_until > now
        ]

    def _exhausted_message(self, only: str | None) -> str:
        if not self.configured():
            return "No AI provider configured. Add a key: `nvx secrets set PUKU_API_KEY`."
        scope = f"Provider {only}" if only else "Every configured provider"
        return f"{scope} is out of rate budget, cooling down or disabled. Check `nvx llm status`."

    # --- health bookkeeping -----------------------------------------------------------

    def _count(self, st: ProviderState) -> None:
        today = utcnow().date().isoformat()
        if st.day != today:
            st.day, st.calls_today = today, 0
        st.calls += 1
        st.calls_today += 1

    def _on_success(self, st: ProviderState, result: LLMResult) -> None:
        self._count(st)
        st.successes += 1
        st.total_latency_ms += result.latency_ms
        st.consecutive_failures = 0
        st.consecutive_rate_limits = 0
        st.cooldown_until = None
        st.last_ok_at = utcnow()

    def _on_failure(self, st: ProviderState, e: LLMError) -> None:
        self._count(st)
        st.failures += 1
        st.consecutive_failures += 1
        st.last_error = f"{e.kind}: {e.message}"[:300]
        now = utcnow()
        if isinstance(e, AuthError):
            st.disabled_reason = "auth"
        elif isinstance(e, ModelNotFound):
            st.disabled_reason = "model"
        elif isinstance(e, RateLimited):
            st.rate_limited += 1
            st.consecutive_rate_limits += 1
            base = e.retry_after if e.retry_after is not None else RATE_LIMIT_BASE_S
            wait = (
                base * (2 ** min(st.consecutive_rate_limits - 1, 4))
                if e.retry_after is None
                else base
            )
            st.cooldown_until = now + timedelta(seconds=min(max(wait, 1.0), MAX_COOLDOWN_S))
        elif isinstance(e, QuotaExhausted):
            st.cooldown_until = now + timedelta(seconds=QUOTA_COOLDOWN_S)
        elif isinstance(e, InvalidOutput):
            if st.consecutive_failures >= 3:  # repeatedly unusable output: rest it a little
                st.cooldown_until = now + timedelta(seconds=60)
        elif e.kind == "bad_request":
            pass  # this request's problem, not the provider's
        else:  # unavailable
            wait = UNAVAILABLE_BASE_S * (2 ** min(st.consecutive_failures - 1, 5))
            wait = min(wait, 300.0)
            if getattr(e, "cooldown_s", None):
                wait = max(wait, e.cooldown_s)  # type: ignore[attr-defined]
            st.cooldown_until = now + timedelta(seconds=wait)

    async def _record(
        self,
        st: ProviderState,
        req: JSONRequest,
        outcome: str,
        latency_ms: int,
        result: LLMResult | None = None,
        error: str | None = None,
    ) -> None:
        if self.recorder is None:
            return
        rec = CallRecord(
            provider=st.spec.id,
            model=result.model if result else (st.provider.model if st.provider else "?"),
            task=req.task,
            outcome=outcome,
            latency_ms=latency_ms,
            input_tokens=result.input_tokens if result else None,
            output_tokens=result.output_tokens if result else None,
            error=error[:300] if error else None,
        )
        with contextlib.suppress(Exception):  # stats must never break a request
            await self.recorder(rec)

    # --- diagnostics ---------------------------------------------------------------

    async def test(self, provider_id: str) -> dict[str, Any]:
        """Send a tiny JSON request to one provider and report the outcome."""
        req = JSONRequest(
            system="You are a health check. Reply with the requested JSON only.",
            user='Return {"ok": true, "word": "neurovex"}.',
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                "required": ["ok", "word"],
                "additionalProperties": False,
            },
            schema_name="health_check",
            max_tokens=300,
            task="llm.test",
        )
        st = self.states.get(provider_id)
        if st is None or not st.configured:
            return {"provider": provider_id, "ok": False, "error": "not configured (no key)"}
        if st.disabled_reason:  # an explicit test is a good reason to try again
            st.disabled_reason = None
        st.cooldown_until = None
        try:
            _, result = await self.complete_json(
                req, parse=_parse_json, only=provider_id, max_wait_s=15
            )
        except ProvidersExhausted:
            return {"provider": provider_id, "ok": False, "error": st.last_error}
        return {
            "provider": provider_id,
            "ok": True,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "json_mode": result.json_mode,
        }

    async def list_models(self, provider_id: str) -> list[str]:
        st = self.states.get(provider_id)
        if st is None or st.provider is None:
            raise ProvidersExhausted(f"{provider_id} is not configured")
        return await st.provider.list_models()

    async def test_all(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(self.test(pid) for pid in self.configured())))


def _parse_json(text: str) -> dict[str, Any]:
    import json

    value = json.loads(text)
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ValueError("unexpected health-check payload")
    return value
