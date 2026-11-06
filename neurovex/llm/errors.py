"""Errors raised by LLM providers, classified so the router knows what to do next."""

from __future__ import annotations


class LLMError(Exception):
    """Base class. `kind` drives the router's cooldown policy."""

    kind = "error"

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


class AuthError(LLMError):
    """Bad or missing key. The provider is disabled until its key changes."""

    kind = "auth"


class RateLimited(LLMError):
    """429. Cool down for `retry_after` seconds, then try again."""

    kind = "rate_limited"

    def __init__(self, provider: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(provider, message)
        self.retry_after = retry_after


class QuotaExhausted(LLMError):
    """402 / out of free credits. Long cooldown."""

    kind = "quota"


class ModelNotFound(LLMError):
    """404 for the configured model: a config problem, disabled until restart."""

    kind = "model"


class BadRequest(LLMError):
    """400/422 that isn't about auth or JSON mode: skip to the next provider."""

    kind = "bad_request"


class Unavailable(LLMError):
    """5xx, timeouts, connection errors: short cooldown, retry later.

    `cooldown_s` overrides the router's escalating default, e.g. for a provider whose
    own upstream is misconfigured (won't fix itself in seconds).
    """

    kind = "unavailable"

    def __init__(self, provider: str, message: str, cooldown_s: float | None = None) -> None:
        super().__init__(provider, message)
        self.cooldown_s = cooldown_s


class InvalidOutput(LLMError):
    """The model answered, but not with valid JSON for the schema (or it was cut off)."""

    kind = "invalid_output"


class ProvidersExhausted(Exception):
    """Every configured provider failed or is cooling down for this request."""

    def __init__(
        self, message: str, retry_in_s: float | None = None, kinds: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.retry_in_s = retry_in_s
        self.kinds = kinds or []  # failure kind per attempted provider (empty = none tried)
