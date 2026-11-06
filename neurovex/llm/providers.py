"""Provider clients: one OpenAI-compatible client for the free cloud APIs,
plus Anthropic (optional, paid) and Ollama (optional, local).
"""

from __future__ import annotations

import json
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from neurovex.core.db import utcnow
from neurovex.llm.catalog import JSONMode, ProviderSpec
from neurovex.llm.errors import (
    AuthError,
    BadRequest,
    InvalidOutput,
    LLMError,
    ModelNotFound,
    QuotaExhausted,
    RateLimited,
    Unavailable,
)
from neurovex.llm.jsonutil import extract_json, schema_instruction
from neurovex.llm.ratelimit import duration_s, parse_rate_headers
from neurovex.llm.types import JSONRequest, LLMResult

_JSON_MODE_HINTS = re.compile(
    r"response_format|json_schema|json_object|structured|schema|json mode|"
    r"no endpoints found|requested parameters",
    re.IGNORECASE,
)
_BAD_GENERATION = re.compile(
    r"failed to validate json|json_validate_failed|failed_generation", re.IGNORECASE
)
_AUTH_HINTS = re.compile(r"api[ _-]?key|unauthori[sz]ed|authenticat|permission", re.IGNORECASE)
_UPSTREAM_AUTH = re.compile(
    # also matches a nested, escaped `\"code\":401` inside an error message
    r'missing authentication|authentication header|code\\?"\s*:\s*401|unauthori[sz]ed',
    re.IGNORECASE,
)
UPSTREAM_COOLDOWN_S = 900.0
_DOWNGRADE = {JSONMode.JSON_SCHEMA: JSONMode.JSON_OBJECT, JSONMode.JSON_OBJECT: JSONMode.PROMPT}


class Provider(Protocol):
    spec: ProviderSpec
    model: str

    @property
    def id(self) -> str: ...

    @property
    def is_local(self) -> bool: ...

    async def complete_json(self, req: JSONRequest) -> LLMResult: ...

    async def list_models(self) -> list[str]: ...


# --- error helpers ---------------------------------------------------------------------


def retry_after_s(resp: httpx.Response) -> float | None:
    h = resp.headers
    if ra := h.get("retry-after"):
        if (s := duration_s(ra)) is not None:
            return s
        try:
            return max(0.0, (parsedate_to_datetime(ra) - utcnow()).total_seconds())
        except (TypeError, ValueError):
            pass
    # Per-minute token resets first: per-day request resets would over-penalise a short limit.
    for name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
        if (s := duration_s(h.get(name))) is not None:
            return max(0.0, s)
    if m := re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text):  # Gemini
        return float(m.group(1))
    # Groq: "... Please try again in 7.66s." / "in 2m59.5s"
    if m := re.search(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", resp.text):
        return duration_s(m.group(1))
    return None


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, list) and body and isinstance(body[0], dict):  # Gemini wraps in a list
        body = body[0]
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        msg = err.get("message") or err.get("detail") or json.dumps(err)[:300]
    elif isinstance(err, list) and err:
        msg = json.dumps(err[0])[:300]
    else:
        msg = str(err)
    if isinstance(body, dict) and body.get("errors"):  # Cloudflare
        msg = "; ".join(e.get("message", "") for e in body["errors"] if isinstance(e, dict)) or msg
    return str(msg)[:300]


def classify_http_error(provider: str, resp: httpx.Response) -> LLMError:
    status, text = resp.status_code, _error_text(resp)
    if status in (401, 403) or (
        status == 400 and _AUTH_HINTS.search(text) and "key" in text.lower()
    ):
        return AuthError(provider, f"HTTP {status}: {text}")
    if status == 429:
        # Monthly/plan quotas (e.g. Puku "chat_req_limit_exceeded") need a long cooldown, not 30 s.
        quota_hint = re.search(
            r"quota|billing|credit|monthly|upgrade your plan", resp.text, re.IGNORECASE
        )
        if quota_hint and "per minute" not in text:
            return QuotaExhausted(provider, f"HTTP 429: {text}")
        return RateLimited(provider, f"HTTP 429: {text}", retry_after_s(resp))
    if status == 402:
        return QuotaExhausted(provider, f"HTTP 402: {text}")
    if status == 404:
        return ModelNotFound(provider, f"HTTP 404: {text}")
    if status == 400 and _BAD_GENERATION.search(resp.text):
        # The model's output broke the schema (Groq "json_validate_failed"). The request
        # was fine, so this is bad output: worth one more try, not a dead end.
        return InvalidOutput(provider, f"HTTP 400: {text}")
    if status in (400, 422):
        return BadRequest(provider, f"HTTP {status}: {text}")
    if status == 413:
        return BadRequest(provider, "HTTP 413: request too large")
    if status >= 500 and _UPSTREAM_AUTH.search(resp.text):
        # The gateway accepted our key but its own upstream rejected it (seen on Puku:
        # 500 wrapping "Missing Authentication header", 401). Provider-side; rest it longer.
        return Unavailable(
            provider, f"HTTP {status} (provider upstream): {text}", UPSTREAM_COOLDOWN_S
        )
    return Unavailable(provider, f"HTTP {status}: {text}")


# --- OpenAI-compatible (Gemini, Groq, OpenRouter, Cerebras, Mistral, Cloudflare, HF) -----


class OpenAICompatProvider:
    path = "/chat/completions"

    def __init__(
        self,
        spec: ProviderSpec,
        model: str,
        api_key: str,
        http: httpx.AsyncClient,
        account_id: str | None = None,
    ) -> None:
        self.spec = spec
        self.model = model
        self._key = api_key
        self._http = http
        self._base = spec.base_url.format(account_id=account_id or "").rstrip("/")
        self.json_mode = spec.json_mode  # may be downgraded at runtime

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def is_local(self) -> bool:
        return False

    def _headers(self) -> dict[str, str]:
        return {
            self.spec.auth_header: f"{self.spec.auth_prefix}{self._key}",
            **self.spec.extra_headers,
        }

    def _body(self, req: JSONRequest, mode: JSONMode) -> dict[str, Any]:
        if req.self_described and mode is JSONMode.JSON_SCHEMA:
            mode = JSONMode.JSON_OBJECT
        system = (
            req.system
            if mode is JSONMode.JSON_SCHEMA or req.self_described
            else req.system + schema_instruction(req.schema)
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": req.user},
            ],
            "max_tokens": req.max_tokens,
            "stream": False,
            **self.spec.extra_body,
        }
        if mode is JSONMode.JSON_SCHEMA:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": req.schema_name,
                    "schema": req.schema,
                    "strict": self.spec.strict,
                },
            }
        elif mode is JSONMode.JSON_OBJECT:
            body["response_format"] = {"type": "json_object"}
        else:
            body.pop("provider", None)  # OpenRouter: nothing special to require in prompt mode
        return body

    async def complete_json(self, req: JSONRequest) -> LLMResult:
        while True:
            mode = self.json_mode
            started = time.perf_counter()
            try:
                resp = await self._http.post(
                    f"{self._base}{self.path}",
                    json=self._body(req, mode),
                    headers=self._headers(),
                )
            except httpx.TimeoutException as e:
                raise Unavailable(self.id, "request timed out") from e
            except httpx.HTTPError as e:
                raise Unavailable(self.id, f"connection error: {type(e).__name__}") from e

            if resp.status_code >= 400:
                err = classify_http_error(self.id, resp)
                # The provider/model doesn't support this JSON mode: step down and retry.
                downgradable = isinstance(
                    err, BadRequest | ModelNotFound
                ) and _JSON_MODE_HINTS.search(err.message)
                if downgradable and mode in _DOWNGRADE:
                    self.json_mode = _DOWNGRADE[mode]
                    continue
                raise err
            return self._parse(resp, mode, started)

    def _parse(self, resp: httpx.Response, mode: JSONMode, started: float) -> LLMResult:
        try:
            data = resp.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise InvalidOutput(self.id, "unexpected response shape") from e
        content = choice.get("message", {}).get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if choice.get("finish_reason") == "length":
            raise InvalidOutput(self.id, "output cut off (max_tokens)")
        if not content:
            raise InvalidOutput(self.id, "empty response")
        try:
            text = extract_json(content)
        except ValueError as e:
            raise InvalidOutput(self.id, str(e)) from e
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            provider=self.id,
            model=data.get("model") or self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            json_mode=mode.value,
            rate=parse_rate_headers(resp.headers),
        )

    async def list_models(self) -> list[str]:
        if self.spec.id == "cloudflare":
            url = self._base.removesuffix("/v1") + "/models/search"
            resp = await self._http.get(
                url, headers=self._headers(), params={"task": "Text Generation"}
            )
            if resp.status_code >= 400:
                raise classify_http_error(self.id, resp)
            return sorted(m.get("name", "") for m in resp.json().get("result", []))
        resp = await self._http.get(f"{self._base}/models", headers=self._headers())
        if resp.status_code >= 400:
            raise classify_http_error(self.id, resp)
        data = resp.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        ids = [m.get("id") or m.get("name") for m in items if isinstance(m, dict)]
        return sorted(i.removeprefix("models/") for i in ids if i)


class MessagesProvider(OpenAICompatProvider):
    """A gateway speaking the Anthropic Messages format over plain HTTP (Puku's /v1/messages)."""

    path = "/messages"

    def _body(self, req: JSONRequest, mode: JSONMode) -> dict[str, Any]:
        return {
            "model": self.model,
            "system": req.system
            if req.self_described
            else req.system + schema_instruction(req.schema),
            "messages": [{"role": "user", "content": req.user}],
            "max_tokens": req.max_tokens,
            **self.spec.extra_body,
        }

    def _parse(self, resp: httpx.Response, mode: JSONMode, started: float) -> LLMResult:
        try:
            data = resp.json()
            blocks = data["content"]
            content = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise InvalidOutput(self.id, "unexpected response shape") from e
        if data.get("stop_reason") == "max_tokens":
            raise InvalidOutput(self.id, "output cut off (max_tokens)")
        if not content:
            raise InvalidOutput(self.id, "empty response")
        try:
            text = extract_json(content)
        except ValueError as e:
            raise InvalidOutput(self.id, str(e)) from e
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            provider=self.id,
            model=data.get("model") or self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            json_mode=mode.value,
            rate=parse_rate_headers(resp.headers),
        )


# --- Anthropic (optional, paid) -----------------------------------------------------------


class AnthropicProvider:
    def __init__(self, spec: ProviderSpec, model: str, client: Any) -> None:
        self.spec = spec
        self.model = model
        self._client = client  # anthropic.AsyncAnthropic or a test double

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def is_local(self) -> bool:
        return False

    async def complete_json(self, req: JSONRequest) -> LLMResult:
        import anthropic

        started = time.perf_counter()
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=req.max_tokens,
                system=req.system,
                messages=[{"role": "user", "content": req.user}],
                output_config={
                    "format": {"type": "json_schema", "schema": req.schema},
                    "effort": "low",
                },
            )
        except anthropic.RateLimitError as e:
            ra = e.response.headers.get("retry-after") if e.response is not None else None
            raise RateLimited(self.id, "rate limited", duration_s(ra)) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise AuthError(self.id, "key rejected") from e
        except anthropic.NotFoundError as e:
            raise ModelNotFound(self.id, f"model {self.model} not found") from e
        except anthropic.BadRequestError as e:
            raise BadRequest(self.id, str(e)[:300]) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 402:
                raise QuotaExhausted(self.id, "billing") from e
            raise Unavailable(self.id, f"HTTP {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise Unavailable(self.id, "connection error") from e

        if resp.stop_reason == "refusal":
            raise BadRequest(self.id, "model declined this request")
        if resp.stop_reason == "max_tokens":
            raise InvalidOutput(self.id, "output cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise InvalidOutput(self.id, "empty response")
        return LLMResult(
            text=text,
            provider=self.id,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            json_mode="json_schema",
        )

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        return sorted(m.id for m in page.data)


# --- Ollama (optional, local development) -----------------------------------------------------


class OllamaProvider:
    def __init__(
        self, spec: ProviderSpec, model: str, base_url: str, http: httpx.AsyncClient
    ) -> None:
        self.spec = spec
        self.model = model
        self._base = base_url.rstrip("/")
        self._http = http

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def is_local(self) -> bool:
        return True

    async def complete_json(self, req: JSONRequest) -> LLMResult:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "stream": False,
            "format": req.schema,
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": req.max_tokens},
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user[:6000]},  # CPU prompt processing is slow
            ],
        }
        try:
            resp = await self._http.post(f"{self._base}/api/chat", json=payload, timeout=300.0)
        except httpx.HTTPError as e:
            raise Unavailable(self.id, f"Ollama not reachable: {type(e).__name__}") from e
        if resp.status_code == 404:
            raise ModelNotFound(self.id, f"model {self.model} not pulled")
        if resp.status_code >= 400:
            raise Unavailable(self.id, f"HTTP {resp.status_code}")
        content = resp.json().get("message", {}).get("content", "")
        try:
            text = extract_json(content)
        except ValueError as e:
            raise InvalidOutput(self.id, str(e)) from e
        return LLMResult(
            text=text,
            provider=self.id,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            json_mode="json_schema",
        )

    async def list_models(self) -> list[str]:
        resp = await self._http.get(f"{self._base}/api/tags", timeout=5.0)
        return sorted(m.get("name", "") for m in resp.json().get("models", []))
