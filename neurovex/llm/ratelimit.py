"""Proactive rate limiting: stay under each provider's limits instead of hitting 429s.

Two sources of truth, combined per provider in a `RateGate`:
- Static hints from the catalog (requests/minute, tokens/minute) for providers that
  don't send rate headers (e.g. Cerebras' token bucket).
- What the provider tells us on every response: `x-ratelimit-remaining-tokens`,
  `x-ratelimit-reset-tokens`, `x-ratelimit-remaining-requests`, ... (Groq, OpenAI-style
  gateways, OpenRouter). The token limit itself is learned from `x-ratelimit-limit-tokens`.

The router asks `wait_s(estimated_tokens)` before using a provider. 0 means go now;
otherwise it tries another provider or waits briefly for capacity.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

WINDOW_S = 60.0


def duration_s(raw: str | None) -> float | None:
    """'7.66s', '2m59.56s', '1h2m', '36s', '120' → seconds. Epoch stamps → seconds from now."""
    if raw is None:
        return None
    raw = raw.strip()
    try:
        value = float(raw)
    except ValueError:
        total, matched = 0.0, False
        for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", raw):
            matched = True
            total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
        return total if matched else None
    # Some providers send an absolute epoch timestamp (ms or s) instead of a duration.
    if value > 1e12:
        return max(0.0, value / 1000 - time.time())
    if value > 1e9:
        return max(0.0, value - time.time())
    return value


@dataclass(frozen=True)
class RateInfo:
    remaining_requests: int | None = None
    reset_requests_s: float | None = None
    remaining_tokens: int | None = None
    reset_tokens_s: float | None = None
    limit_tokens: int | None = None

    @property
    def empty(self) -> bool:
        return all(v is None for v in self.__dict__.values())


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


def _first(headers: Mapping[str, str], *names: str) -> str | None:
    for n in names:
        if (v := headers.get(n)) is not None:
            return v
    return None


def parse_rate_headers(headers: Mapping[str, str]) -> RateInfo | None:
    """Read the common x-ratelimit-* header families (case-insensitive mapping expected)."""
    info = RateInfo(
        remaining_requests=_int(
            _first(
                headers,
                "x-ratelimit-remaining-requests",
                "x-ratelimit-remaining-requests-day",
                "x-ratelimit-remaining",
            )  # fmt: skip
        ),
        reset_requests_s=duration_s(
            _first(
                headers,
                "x-ratelimit-reset-requests",
                "x-ratelimit-reset-requests-day",
                "x-ratelimit-reset",
            )  # fmt: skip
        ),
        remaining_tokens=_int(
            _first(headers, "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-tokens-minute")
        ),
        reset_tokens_s=duration_s(
            _first(headers, "x-ratelimit-reset-tokens", "x-ratelimit-reset-tokens-minute")
        ),
        limit_tokens=_int(
            _first(headers, "x-ratelimit-limit-tokens", "x-ratelimit-limit-tokens-minute")
        ),
    )
    return None if info.empty else info


def prompt_tokens(system: str, user: str) -> int:
    """Rough prompt size (~3.6 characters per token)."""
    return int((len(system) + len(user)) / 3.6)


def estimate_tokens(system: str, user: str, max_tokens: int) -> int:
    """Request size as providers count it up front. Groq charges prompt + the full
    `max_tokens` against its per-minute budget when a request starts, however short the
    answer turns out, so `max_tokens` should be sized to real outputs (see callers)."""
    return prompt_tokens(system, user) + max_tokens


class RateGate:
    """Sliding one-minute window of requests and tokens, plus learned provider state."""

    def __init__(
        self,
        rpm: int | None = None,
        tpm: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rpm = rpm
        self.tpm = tpm
        self._clock = clock
        self._events: deque[list[float]] = deque()  # [started_at, tokens]
        self.blocked_until = 0.0
        self.remaining_tokens: int | None = None
        self._tokens_reset_at = 0.0

    def _trim(self, now: float) -> None:
        while self._events and self._events[0][0] <= now - WINDOW_S:
            self._events.popleft()

    def wait_s(self, est_tokens: int) -> float:
        """Seconds until a request of `est_tokens` fits (0 = now)."""
        now = self._clock()
        self._trim(now)
        waits = [self.blocked_until - now]
        if self.rpm and len(self._events) >= self.rpm:
            waits.append(self._events[len(self._events) - self.rpm][0] + WINDOW_S - now)
        if self.tpm:
            used = sum(e[1] for e in self._events)
            if used + est_tokens > self.tpm and self._events:
                freed = 0.0
                for started, tokens in self._events:  # oldest first
                    freed += tokens
                    if used - freed + est_tokens <= self.tpm:
                        waits.append(started + WINDOW_S - now)
                        break
                else:  # bigger than the whole budget: wait for a clean window
                    waits.append(self._events[-1][0] + WINDOW_S - now)
        if (
            self.remaining_tokens is not None
            and now < self._tokens_reset_at
            and est_tokens > self.remaining_tokens
        ):
            waits.append(self._tokens_reset_at - now)
        return max(0.0, *waits)

    def reserve(self, est_tokens: int) -> list[float]:
        """Count a request that is starting now. Returns a handle for `settle`."""
        event = [self._clock(), float(est_tokens)]
        self._events.append(event)
        if self.remaining_tokens is not None:
            self.remaining_tokens -= est_tokens
        return event

    @staticmethod
    def settle(handle: list[float], actual_tokens: int | None) -> None:
        if actual_tokens is not None:
            handle[1] = float(max(actual_tokens, 0))

    def observe(self, info: RateInfo | None) -> None:
        """Update from a provider's rate headers."""
        if info is None:
            return
        now = self._clock()
        if info.limit_tokens:
            self.tpm = info.limit_tokens  # the provider knows best
        if info.remaining_tokens is not None:
            self.remaining_tokens = info.remaining_tokens
            self._tokens_reset_at = now + (info.reset_tokens_s or WINDOW_S)
        if info.remaining_requests is not None and info.remaining_requests <= 0:
            self.blocked_until = max(self.blocked_until, now + (info.reset_requests_s or WINDOW_S))

    def snapshot(self, est_tokens: int = 2000) -> dict[str, float | int | None]:
        now = self._clock()
        self._trim(now)
        return {
            "requests_last_min": len(self._events),
            "tokens_last_min": int(sum(e[1] for e in self._events)),
            "rpm_limit": self.rpm,
            "tpm_limit": self.tpm,
            "remaining_tokens": self.remaining_tokens if now < self._tokens_reset_at else None,
            "next_slot_s": round(self.wait_s(est_tokens), 1),
        }
