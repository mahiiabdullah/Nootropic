"""The providers Neurovex knows, with defaults verified against their docs on 2026-10-10.

Every value here can be overridden in settings (model per provider, order,
concurrency). Free tiers and model names change often: run `nvx llm models <id>`
to see what a provider offers today, and `nvx llm test` to check your keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JSONMode(StrEnum):
    JSON_SCHEMA = "json_schema"  # response_format json_schema (strict where supported)
    JSON_OBJECT = "json_object"  # valid JSON guaranteed, schema described in the prompt
    PROMPT = "prompt"  # no response_format; schema in the prompt, JSON extracted from text


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    base_url: str  # may contain {account_id}
    key_env: str  # primary secret name (env var / keychain entry)
    default_model: str
    json_mode: JSONMode = JSONMode.JSON_SCHEMA
    strict: bool = True
    alt_key_envs: tuple[str, ...] = ()
    account_env: str | None = None  # Cloudflare needs an account id too
    max_concurrency: int = 2
    extra_headers: dict[str, str] = field(default_factory=dict)
    auth_header: str = "Authorization"  # Puku uses X-Api-Key
    # Static rate hints for proactive pacing (None = learn from response headers only).
    rpm: int | None = None
    tpm: int | None = None
    auth_prefix: str = "Bearer "
    extra_body: dict[str, Any] = field(default_factory=dict)
    signup_url: str = ""
    free_tier: str = ""
    kind: str = "openai"  # openai | messages (Anthropic format over HTTP) | anthropic | ollama


PROVIDERS: dict[str, ProviderSpec] = {
    "cerebras": ProviderSpec(
        id="cerebras",
        name="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        key_env="CEREBRAS_API_KEY",
        default_model="gpt-oss-120b",
        extra_body={"reasoning_effort": "low"},
        signup_url="https://cloud.cerebras.ai",
        free_tier="Free tier with daily token limits; very fast inference",
    ),
    "groq": ProviderSpec(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        default_model="openai/gpt-oss-120b",  # strict json_schema supported
        rpm=30,
        tpm=8000,  # free tier; replaced by x-ratelimit-limit-tokens after the first call
        extra_body={"reasoning_effort": "low"},
        signup_url="https://console.groq.com/keys",
        free_tier="Free tier with per-model RPM/RPD limits",
    ),
    "gemini": ProviderSpec(
        id="gemini",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        alt_key_envs=("GOOGLE_API_KEY",),
        default_model="gemini-3.8-flash",
        strict=False,
        extra_body={"reasoning_effort": "low"},
        signup_url="https://aistudio.google.com/apikey",
        free_tier="Free tier per project; daily quota resets at midnight Pacific",
    ),
    "mistral": ProviderSpec(
        id="mistral",
        name="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        key_env="MISTRAL_API_KEY",
        default_model="mistral-small-latest",
        signup_url="https://console.mistral.ai/api-keys",
        free_tier="Free 'Experiment' plan with rate limits",
    ),
    "openrouter": ProviderSpec(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        default_model="nvidia/nemotron-3-super-120b-a12b:free",
        max_concurrency=1,
        extra_headers={"X-Title": "Neurovex"},
        # Only route to endpoints that honour response_format.
        extra_body={"provider": {"require_parameters": True}},
        signup_url="https://openrouter.ai/keys",
        free_tier=":free models: 20 req/min, 50 req/day (1,000/day after $10 of credits)",
    ),
    "cloudflare": ProviderSpec(
        id="cloudflare",
        name="Cloudflare Workers AI",
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        key_env="CLOUDFLARE_API_TOKEN",
        account_env="CLOUDFLARE_ACCOUNT_ID",
        default_model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        # JSON mode on the OpenAI-compatible endpoint is not documented: ask in the prompt.
        json_mode=JSONMode.PROMPT,
        signup_url="https://dash.cloudflare.com/profile/api-tokens",
        free_tier="10,000 neurons/day free",
    ),
    "huggingface": ProviderSpec(
        id="huggingface",
        name="Hugging Face Inference Providers",
        base_url="https://router.huggingface.co/v1",
        key_env="HF_TOKEN",
        alt_key_envs=("HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"),
        default_model="openai/gpt-oss-120b:fastest",
        strict=False,
        signup_url="https://huggingface.co/settings/tokens",
        free_tier="Small monthly credits on free accounts (more with PRO)",
    ),
    "puku": ProviderSpec(
        id="puku",
        name="Puku AI",
        # Authenticated with X-Api-Key (api.puku.sh is the editor's PKCE login, not this API).
        # Uses /v1/messages: on 2026-11-04 /v1/chat/completions still failed upstream (HTTP 500
        # wrapping "Missing Authentication header") while /v1/messages answered normally.
        kind="messages",
        extra_headers={"anthropic-version": "2023-06-01"},
        base_url="https://api.sdk.puku.sh/v1",
        key_env="PUKU_API_KEY",
        default_model="puku-ai-2.8",
        # Structured-output support isn't documented: ask for JSON in the prompt.
        json_mode=JSONMode.PROMPT,
        auth_header="X-Api-Key",
        auth_prefix="",
        signup_url="https://puku.sh",
        free_tier="Monthly chat-request quota set by your Puku plan (0 on some plans)",
    ),
    # Optional, not free: used only if listed in NEUROVEX_LLM_PROVIDERS and a key exists.
    "anthropic": ProviderSpec(
        id="anthropic",
        name="Anthropic Claude",
        base_url="https://api.anthropic.com",
        key_env="ANTHROPIC_API_KEY",
        default_model="claude-haiku-5-5",
        max_concurrency=4,
        signup_url="https://console.anthropic.com",
        free_tier="Paid",
        kind="anthropic",
    ),
    # Local development only (CPU): used only if listed in NEUROVEX_LLM_PROVIDERS.
    "ollama": ProviderSpec(
        id="ollama",
        name="Ollama (local)",
        base_url="http://localhost:11434",
        key_env="",
        default_model="qwen2.5",
        max_concurrency=1,
        free_tier="Local",
        kind="ollama",
    ),
}

FREE_PROVIDERS = (
    "cerebras",
    "groq",
    "gemini",
    "mistral",
    "openrouter",
    "cloudflare",
    "huggingface",
)
# Default chain: Puku only (the user's choice). The other providers stay available: add
# their ids to NEUROVEX_LLM_PROVIDERS to switch them on.
DEFAULT_PROVIDERS = ("puku",)
DEFAULT_ORDER = ",".join(DEFAULT_PROVIDERS)
