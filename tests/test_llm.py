from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from neurovex.core.config import Settings
from neurovex.core.db import utcnow
from neurovex.llm.catalog import PROVIDERS, JSONMode
from neurovex.llm.errors import (
    AuthError,
    BadRequest,
    InvalidOutput,
    ModelNotFound,
    ProvidersExhausted,
    QuotaExhausted,
    RateLimited,
    Unavailable,
)
from neurovex.llm.jsonutil import extract_json
from neurovex.llm.providers import (
    AnthropicProvider,
    MessagesProvider,
    OllamaProvider,
    OpenAICompatProvider,
    classify_http_error,
    retry_after_s,
)
from neurovex.llm.router import LLMRouter, build_provider
from neurovex.llm.types import JSONRequest, LLMResult

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
REQ = JSONRequest(system="sys", user="hi", schema=SCHEMA, schema_name="t", max_tokens=100)
OK = '{"ok": true}'


def completion(content, finish="stop", model="m-1"):
    return {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


def message(text, stop_reason="end_turn", model="puku-ai-2.8"):
    """An Anthropic-format response, as Puku's /v1/messages returns it."""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 11, "output_tokens": 3},
    }


# --- fakes shared with test_summarizer -----------------------------------------------------


class FakeProvider:
    """Scripted provider: each call pops the next item (JSON text or an exception)."""

    def __init__(self, spec, script, delay: float = 0.0):
        self.spec = spec
        self.model = f"fake-{spec.id}"
        self.script = list(script)
        self.delay = delay
        self.calls = 0
        self.requests: list[JSONRequest] = []

    @property
    def id(self):
        return self.spec.id

    @property
    def is_local(self):
        return self.spec.kind == "ollama"

    async def complete_json(self, req):
        self.calls += 1
        self.requests.append(req)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if self.script else OK
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=item, provider=self.spec.id, model=self.model, latency_ms=5)

    async def list_models(self):
        return ["a", "b"]


def fake_builder(scripts: dict, delays: dict | None = None, keys: dict | None = None):
    made: dict[str, FakeProvider] = {}

    def build(spec, settings, http):
        if spec.id not in scripts:
            return None, None
        if spec.id not in made:
            made[spec.id] = FakeProvider(spec, scripts[spec.id], (delays or {}).get(spec.id, 0))
        return made[spec.id], (keys or {}).get(spec.id, "key")

    build.made = made  # type: ignore[attr-defined]
    return build


def router_for(settings: Settings, scripts: dict, **kw) -> LLMRouter:
    s = settings.model_copy(update={"llm_providers": ",".join(scripts), **kw.pop("extra", {})})
    return LLMRouter(s, httpx.AsyncClient(), builder=fake_builder(scripts, **kw))


# --- json extraction & error classification -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        OK,
        '```json\n{"ok": true}\n```',
        'Sure! Here it is: {"ok": true} Hope that helps.',
        '{"note": "braces } inside { strings", "ok": true}',
    ],
)
def test_extract_json(text):
    assert json.loads(extract_json(text))["ok"] is True


def test_extract_json_none():
    with pytest.raises(ValueError):
        extract_json("no json here {")


def resp(status, body=None, headers=None, text=None):
    req = httpx.Request("POST", "https://x.test")
    if text is not None:
        return httpx.Response(status, text=text, headers=headers, request=req)
    return httpx.Response(status, json=body or {}, headers=headers, request=req)


def test_retry_after_parsing():
    assert retry_after_s(resp(429, headers={"retry-after": "12"})) == 12
    assert retry_after_s(resp(429, headers={"x-ratelimit-reset-requests": "2m59.5s"})) == 179.5
    gemini = {"error": {"details": [{"retryDelay": "36s"}]}}
    assert retry_after_s(resp(429, gemini)) == 36
    assert retry_after_s(resp(429)) is None


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, {"error": {"message": "Invalid API Key"}}, AuthError),
        (403, {"errors": [{"message": "Authentication error"}]}, AuthError),
        (400, {"error": {"message": "Please pass a valid API key"}}, AuthError),  # Gemini
        (429, {"error": {"message": "Rate limit reached for requests per minute"}}, RateLimited),
        (
            429,
            {"error": {"message": "You exceeded your current quota, check billing"}},
            QuotaExhausted,
        ),
        (402, {"error": {"message": "Insufficient credits"}}, QuotaExhausted),
        (404, {"error": {"message": "model not found"}}, ModelNotFound),
        (400, {"error": {"message": "messages: field required"}}, BadRequest),
        (  # Groq: the model broke the JSON schema; the request itself was fine
            400,
            {
                "error": {
                    "message": "Failed to validate JSON. See 'failed_generation'.",
                    "code": "json_validate_failed",
                }
            },
            InvalidOutput,
        ),  # fmt: skip
        (503, {"error": {"message": "overloaded"}}, Unavailable),
    ],
)
def test_classify_http_error(status, body, kind):
    assert isinstance(classify_http_error("p", resp(status, body)), kind)


# --- OpenAI-compatible provider ----------------------------------------------------------


@respx.mock
async def test_openai_compat_request_shape_and_parse():
    spec = PROVIDERS["groq"]
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200, json=completion(OK, model="openai/gpt-oss-120b")
    )
    async with httpx.AsyncClient() as http:
        p = OpenAICompatProvider(spec, "openai/gpt-oss-120b", "gsk_test", http)
        result = await p.complete_json(REQ)
    body = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["Authorization"] == "Bearer gsk_test"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"] == {"name": "t", "schema": SCHEMA, "strict": True}
    assert body["reasoning_effort"] == "low" and body["max_tokens"] == 100
    assert body["messages"][0] == {"role": "system", "content": "sys"}  # no schema text needed
    assert (result.text, result.input_tokens, result.json_mode) == (OK, 11, "json_schema")
    assert result.label == "groq/openai/gpt-oss-120b"


@respx.mock
async def test_self_described_requests_send_no_schema():
    """Chat's prompt already shows the reply format: the schema would only cost tokens."""
    req = JSONRequest(system="sys", user="hi", schema=SCHEMA, max_tokens=50, self_described=True)
    groq = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200, json=completion(OK)
    )
    puku = respx.post("https://api.sdk.puku.sh/v1/messages").respond(200, json=message(OK))
    async with httpx.AsyncClient() as http:
        await OpenAICompatProvider(PROVIDERS["groq"], "m", "k", http).complete_json(req)
        await MessagesProvider(PROVIDERS["puku"], "m", "k", http).complete_json(req)
    sent_groq = json.loads(groq.calls[0].request.content)
    sent_puku = json.loads(puku.calls[0].request.content)
    assert sent_groq["response_format"] == {"type": "json_object"}
    assert sent_groq["messages"][0]["content"] == "sys"  # no schema text either
    assert "response_format" not in sent_puku and sent_puku["system"] == "sys"


@respx.mock
async def test_openai_compat_downgrades_json_mode_and_remembers():
    spec = PROVIDERS["mistral"]
    route = respx.post("https://api.mistral.ai/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            400, json={"error": {"message": "response_format json_schema not supported"}}
        ),
        httpx.Response(200, json=completion(OK)),
        httpx.Response(200, json=completion(OK)),
    ]
    async with httpx.AsyncClient() as http:
        p = OpenAICompatProvider(spec, "mistral-small-latest", "k", http)
        await p.complete_json(REQ)
        await p.complete_json(REQ)
    second = json.loads(route.calls[1].request.content)
    assert second["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in second["messages"][0]["content"]
    assert p.json_mode is JSONMode.JSON_OBJECT and route.call_count == 3


@respx.mock
async def test_prompt_mode_extracts_json_and_cloudflare_url():
    spec = PROVIDERS["cloudflare"]
    route = respx.post(
        "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1/chat/completions"
    ).respond(200, json=completion('Here you go:\n```json\n{"ok": true}\n```'))
    async with httpx.AsyncClient() as http:
        p = OpenAICompatProvider(spec, spec.default_model, "cf", http, account_id="acc123")
        result = await p.complete_json(REQ)
    body = json.loads(route.calls[0].request.content)
    assert "response_format" not in body and json.loads(result.text) == {"ok": True}


@respx.mock
async def test_openrouter_headers_and_require_parameters():
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").respond(
        200, json=completion(OK)
    )
    async with httpx.AsyncClient() as http:
        p = OpenAICompatProvider(PROVIDERS["openrouter"], "x:free", "or", http)
        await p.complete_json(REQ)
    req = route.calls[0].request
    assert req.headers["X-Title"] == "Neurovex"
    assert json.loads(req.content)["provider"] == {"require_parameters": True}


@respx.mock
@pytest.mark.parametrize(
    ("payload", "err"),
    [
        (completion("{", finish="length"), InvalidOutput),
        (completion(None), InvalidOutput),
        (completion("I cannot do that."), InvalidOutput),
        ({"unexpected": True}, InvalidOutput),
    ],
)
async def test_openai_compat_bad_outputs(payload, err):
    respx.post("https://api.cerebras.ai/v1/chat/completions").respond(200, json=payload)
    async with httpx.AsyncClient() as http:
        with pytest.raises(err):
            await OpenAICompatProvider(PROVIDERS["cerebras"], "m", "k", http).complete_json(REQ)


@respx.mock
async def test_openai_compat_timeout_is_unavailable():
    respx.post("https://api.cerebras.ai/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(Unavailable):
            await OpenAICompatProvider(PROVIDERS["cerebras"], "m", "k", http).complete_json(REQ)


@respx.mock
async def test_list_models():
    respx.get("https://router.huggingface.co/v1/models").respond(
        200, json={"data": [{"id": "b/model"}, {"id": "a/model"}]}
    )
    respx.get("https://generativelanguage.googleapis.com/v1beta/openai/models").respond(
        200, json={"data": [{"id": "models/gemini-3.8-flash"}]}
    )
    async with httpx.AsyncClient() as http:
        hf = OpenAICompatProvider(PROVIDERS["huggingface"], "m", "k", http)
        gem = OpenAICompatProvider(PROVIDERS["gemini"], "m", "k", http)
        assert await hf.list_models() == ["a/model", "b/model"]
        assert await gem.list_models() == ["gemini-3.8-flash"]


# --- Anthropic & Ollama ---------------------------------------------------------------------


class FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_anthropic(text, stop="end_turn"):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    r = SimpleNamespace(
        content=content, stop_reason=stop, usage=SimpleNamespace(input_tokens=9, output_tokens=2)
    )
    return SimpleNamespace(messages=FakeMessages(r))


async def test_anthropic_provider_structured_output():
    client = fake_anthropic(OK)
    result = await AnthropicProvider(
        PROVIDERS["anthropic"], "claude-haiku-5-5", client
    ).complete_json(REQ)
    assert result.text == OK and result.input_tokens == 9
    kw = client.messages.kwargs
    assert kw["output_config"]["format"]["schema"] is SCHEMA and "temperature" not in kw


@pytest.mark.parametrize(
    ("text", "stop", "err"),
    [
        (None, "refusal", BadRequest),
        ("{", "max_tokens", InvalidOutput),
        (None, "end_turn", InvalidOutput),
    ],
)
async def test_anthropic_provider_errors(text, stop, err):
    with pytest.raises(err):
        await AnthropicProvider(
            PROVIDERS["anthropic"], "m", fake_anthropic(text, stop)
        ).complete_json(REQ)


@respx.mock
async def test_ollama_provider():
    route = respx.post("http://ollama.test/api/chat").respond(
        200, json={"message": {"content": OK}}
    )
    async with httpx.AsyncClient() as http:
        result = await OllamaProvider(
            PROVIDERS["ollama"], "qwen2.5", "http://ollama.test", http
        ).complete_json(REQ)
    assert result.text == OK and json.loads(route.calls[0].request.content)["format"] == SCHEMA


# --- settings & provider construction -----------------------------------------------------


def test_settings_order_and_models(settings):
    s = settings.model_copy(update={"groq_model": "x/y"})
    assert s.llm_provider_order() == list(Settings.model_fields["llm_providers"].default.split(","))
    assert s.model_for("groq") == "x/y" and s.model_for("cerebras") is None
    with pytest.raises(ValueError):
        Settings(_env_file=None, llm_providers="groq,nope")
    with pytest.raises(ValueError):
        Settings(_env_file=None, news_llm_provider="nope")


def test_secret_lookup_env_and_prefix(settings, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "  g1  ")
    monkeypatch.setenv("NEUROVEX_CEREBRAS_API_KEY", "c1")
    monkeypatch.setenv("GOOGLE_API_KEY", "goog")
    assert settings.secret("GROQ_API_KEY") == "g1"
    assert settings.secret("CEREBRAS_API_KEY") == "c1"
    assert settings.secret("GEMINI_API_KEY", "GOOGLE_API_KEY") == "goog"
    assert settings.secret("MISTRAL_API_KEY") is None


async def test_build_provider_from_env(settings, monkeypatch):
    async with httpx.AsyncClient() as http:
        assert build_provider(PROVIDERS["groq"], settings, http) == (None, None)
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf")
        assert build_provider(PROVIDERS["cloudflare"], settings, http)[0] is None  # no account id
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc9")
        p, fp = build_provider(PROVIDERS["cloudflare"], settings, http)
        assert isinstance(p, OpenAICompatProvider) and "acc9" in p._base and fp
        monkeypatch.setenv("HF_TOKEN", "hf_x")
        p, _ = build_provider(
            PROVIDERS["huggingface"], settings.model_copy(update={"huggingface_model": "a/b"}), http
        )
        assert p.model == "a/b"


# --- router ----------------------------------------------------------------------------------


def parse_ok(text):
    value = json.loads(text)
    if value.get("ok") is not True:
        raise ValueError("bad")
    return value


async def test_router_priority_and_failover_on_rate_limit(settings):
    r = router_for(settings, {"groq": [RateLimited("groq", "slow", 20)], "cerebras": [OK]})
    value, result = await r.complete_json(REQ, parse_ok)
    assert value == {"ok": True} and result.provider == "cerebras"
    assert result.attempts == ["groq: rate_limited"]
    groq = r.states["groq"]
    assert groq.cooldown_until is not None and groq.rate_limited == 1
    # While groq cools down, it is skipped without being called.
    await r.complete_json(REQ, parse_ok)
    assert r._builder.made["groq"].calls == 1  # type: ignore[attr-defined]


async def test_router_disables_bad_keys_and_reenables_on_key_change(settings):
    keys = {"groq": "old"}
    r = router_for(
        settings, {"groq": [AuthError("groq", "bad key"), OK], "cerebras": [OK]}, keys=keys
    )
    _, result = await r.complete_json(REQ, parse_ok)
    assert result.provider == "cerebras" and r.states["groq"].disabled_reason == "auth"
    keys["groq"] = "new"
    r.refresh()
    assert r.states["groq"].disabled_reason is None


async def test_router_invalid_output_tries_next_provider(settings):
    r = router_for(settings, {"groq": ['{"ok": false}', '{"ok": false}'], "cerebras": [OK]})
    _, result = await r.complete_json(REQ, parse_ok)
    assert result.provider == "cerebras" and r.states["groq"].cooldown_until is None
    assert result.attempts == ["groq: invalid_output", "groq: invalid_output"]


async def test_router_retries_one_bad_output_on_the_same_provider(settings):
    """With one working provider, a single malformed answer must not sink the request."""
    r = router_for(settings, {"groq": ['{"ok": false}', OK]})
    _, result = await r.complete_json(REQ, parse_ok)
    assert result.provider == "groq" and result.attempts == ["groq: invalid_output"]


async def test_failed_attempts_release_their_rate_reservation(settings):
    r = router_for(settings, {"groq": [RateLimited("groq", "TPM", 1)]})
    with pytest.raises(ProvidersExhausted):
        await r.complete_json(REQ, parse_ok)
    assert r.states["groq"].gate.snapshot()["tokens_last_min"] == 0  # refused: nothing used
    r = router_for(settings, {"groq": [Unavailable("groq", "503")] * 3})
    with pytest.raises(ProvidersExhausted):
        await r.complete_json(REQ, parse_ok)
    used = r.states["groq"].gate.snapshot()["tokens_last_min"]
    assert 0 < used < 3 * min(REQ.max_tokens, 900)  # prompt only, no output reserve


async def test_router_exhausted_reports_kinds_and_retry(settings):
    r = router_for(
        settings,
        {"groq": [RateLimited("groq", "x", 30)], "cerebras": [Unavailable("cerebras", "503")]},
    )
    with pytest.raises(ProvidersExhausted) as exc:
        await r.complete_json(REQ, parse_ok)
    assert exc.value.kinds == ["rate_limited", "unavailable"] and exc.value.retry_in_s
    with pytest.raises(ProvidersExhausted) as exc:  # everything cooling down now
        await r.complete_json(REQ, parse_ok)
    assert exc.value.kinds == [] and "cooling down" in str(exc.value)


async def test_router_no_providers(settings):
    r = router_for(settings, {})
    with pytest.raises(ProvidersExhausted, match="No AI provider configured"):
        await r.complete_json(REQ, parse_ok)


async def test_router_overflows_to_next_provider_under_load(settings):
    r = router_for(
        settings,
        {"groq": [OK, OK], "cerebras": [OK, OK]},
        delays={"groq": 0.05, "cerebras": 0.05},
        extra={"llm_max_concurrency": 1},
    )
    results = await asyncio.gather(*(r.complete_json(REQ, parse_ok) for _ in range(2)))
    assert sorted(res.provider for _, res in results) == ["cerebras", "groq"]


async def test_router_round_robin(settings):
    r = router_for(
        settings, {"groq": [OK] * 3, "cerebras": [OK] * 3}, extra={"llm_strategy": "round_robin"}
    )
    providers = [(await r.complete_json(REQ, parse_ok))[1].provider for _ in range(4)]
    assert providers == ["groq", "cerebras", "groq", "cerebras"]


async def test_router_records_every_attempt_and_reports_status(settings):
    records = []

    async def recorder(rec):
        records.append(rec)

    s = settings.model_copy(update={"llm_providers": "groq,cerebras"})
    r = LLMRouter(s, httpx.AsyncClient(), recorder=recorder,
                  builder=fake_builder({"groq": [QuotaExhausted("groq", "credits")], "cerebras": [OK]}))  # fmt: skip
    await r.complete_json(REQ, parse_ok)
    assert [(x.provider, x.outcome) for x in records] == [("groq", "quota"), ("cerebras", "ok")]
    status = {st["id"]: st for st in r.status()}
    assert status["groq"]["status"] == "cooling_down" and status["cerebras"]["successes"] == 1
    assert r.label() == "groq/fake-groq +1 fallback"


async def test_router_test_and_models(settings):
    r = router_for(settings, {"groq": [OK]})
    report = await r.test("groq")
    assert report["ok"] and report["provider"] == "groq"
    assert (await r.test("mistral"))["ok"] is False
    assert await r.list_models("groq") == ["a", "b"]


def test_gemini_list_wrapped_error_message():
    body = [
        {
            "error": {
                "code": 400,
                "message": "Please pass a valid API key",
                "status": "INVALID_ARGUMENT",
            }
        }
    ]
    err = classify_http_error("gemini", resp(400, body))
    assert isinstance(err, AuthError) and err.message == "HTTP 400: Please pass a valid API key"


@respx.mock
async def test_puku_uses_messages_endpoint_with_x_api_key_and_prompt_json():
    route = respx.post("https://api.sdk.puku.sh/v1/messages").respond(
        200, json=message('```json\n{"ok": true}\n```')
    )
    async with httpx.AsyncClient() as http:
        p = MessagesProvider(PROVIDERS["puku"], "puku-ai-2.8", "pk_live_x", http)
        result = await p.complete_json(REQ)
    req = route.calls[0].request
    assert req.headers["X-Api-Key"] == "pk_live_x" and "Authorization" not in req.headers
    body = json.loads(req.content)
    assert "response_format" not in body and "JSON Schema" in body["system"]
    assert body["messages"] == [{"role": "user", "content": "hi"}] and body["max_tokens"] == 100
    assert json.loads(result.text) == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (11, 3)


async def test_build_provider_picks_the_messages_client_for_puku(settings, monkeypatch):
    monkeypatch.setenv("PUKU_API_KEY", "pk_live_x")
    async with httpx.AsyncClient() as http:
        provider, _ = build_provider(PROVIDERS["puku"], settings, http)
    assert isinstance(provider, MessagesProvider)


def test_plan_quota_429_is_quota_not_rate_limit():
    body = {
        "error": {
            "message": "Monthly chat request limit reached (0/0). Upgrade your plan for more.",
            "type": "quota_exceeded",
            "code": "chat_req_limit_exceeded",
        }
    }
    assert isinstance(classify_http_error("puku", resp(429, body)), QuotaExhausted)


def test_puku_is_the_only_default_provider(settings):
    assert settings.llm_provider_order() == ["puku"]


@respx.mock
async def test_messages_provider_reports_cut_off_and_bad_output():
    route = respx.post("https://api.sdk.puku.sh/v1/messages")
    route.side_effect = [
        httpx.Response(200, json=message(OK, stop_reason="max_tokens")),
        httpx.Response(200, json=message("no json here")),
        httpx.Response(200, json={"unexpected": True}),
    ]
    async with httpx.AsyncClient() as http:
        p = MessagesProvider(PROVIDERS["puku"], "m", "k", http)
        for reason in ("cut off", "JSON|json", "response shape"):
            with pytest.raises(InvalidOutput, match=reason):
                await p.complete_json(REQ)


def test_upstream_auth_failure_is_provider_side_with_long_cooldown(settings):
    # Exact body Puku returned on 2026-10-14: its own upstream rejected the request.
    body = {
        "error": {
            "message": '{"error":{"message":"Missing Authentication header","code":401}}',
            "type": "api_error",
            "status": 401,
        }
    }
    err = classify_http_error("puku", resp(500, body))
    assert isinstance(err, Unavailable) and err.cooldown_s == 900
    r = router_for(settings, {"puku": []})
    r._on_failure(r.states["puku"], err)
    assert (r.states["puku"].cooldown_until - utcnow()).total_seconds() > 800
    assert r.states["puku"].disabled_reason is None  # not the user's key: recovers by itself
    plain = classify_http_error("p", resp(503, {"error": {"message": "overloaded"}}))
    assert isinstance(plain, Unavailable) and plain.cooldown_s is None
