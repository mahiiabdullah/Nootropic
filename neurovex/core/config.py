"""Neurovex settings, loaded from environment variables and `.env`.

Every setting can be overridden with an env var of the same name prefixed with
`NEUROVEX_` (e.g. `NEUROVEX_PORT=9000`).

Secrets (API keys) are looked up by their standard names, e.g. `PUKU_API_KEY`:
process environment → `NEUROVEX_<NAME>` → `.env` file → OS keychain (Windows
Credential Manager). On a server, use environment variables and set
`NEUROVEX_USE_KEYRING=false`.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from neurovex.llm.catalog import DEFAULT_ORDER, PROVIDERS

KEYRING_SERVICE = "neurovex"
ENV_FILE = ".env"


class Profile(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "Neurovex"


def _parse_order(raw: str) -> list[str]:
    ids = [p.strip().lower() for p in raw.split(",") if p.strip()]
    unknown = [p for p in ids if p not in PROVIDERS]
    if unknown:
        raise ValueError(f"Unknown LLM provider(s) {unknown}; known: {', '.join(PROVIDERS)}")
    return list(dict.fromkeys(ids))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEUROVEX_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    profile: Profile = Profile.DEV
    data_dir: Path = Field(default_factory=_default_data_dir)
    log_level: str = "INFO"
    use_keyring: bool = True
    # IANA time zone, e.g. "Asia/Dhaka". Empty = this computer's. Set it on servers.
    timezone: str = ""

    # API: localhost only unless explicitly allowed.
    host: str = "127.0.0.1"
    port: int = 8765
    allow_remote: bool = False

    # --- LLM providers ---------------------------------------------------------------
    # Order = priority. Free cloud providers by default; add "anthropic" (paid) or
    # "ollama" (local dev) to the list to use them.
    llm_providers: str = DEFAULT_ORDER
    llm_strategy: Literal["priority", "round_robin"] = "priority"
    llm_timeout_s: float = Field(default=60.0, ge=5)
    llm_max_concurrency: int | None = Field(
        default=None, ge=1
    )  # per provider; None = catalog default
    # Model per provider (None = catalog default, see neurovex/llm/catalog.py)
    cerebras_model: str | None = None
    groq_model: str | None = None
    gemini_model: str | None = None
    mistral_model: str | None = None
    openrouter_model: str | None = None
    cloudflare_model: str | None = None
    huggingface_model: str | None = None
    puku_model: str | None = None
    anthropic_model: str | None = None
    ollama_model: str = "qwen2.5"
    ollama_url: str = "http://localhost:11434"

    # --- News module ----------------------------------------------------------------
    # "auto" = the provider chain above, "none" = headlines only, or one provider id.
    news_llm_provider: str = "auto"
    news_auto_refresh: bool = True
    # Feeds are polled with conditional GET, so frequent checks are cheap.
    news_refresh_minutes: int = Field(default=10, ge=2)
    news_first_refresh_delay_s: int = Field(default=10, ge=0)
    news_max_age_days: int = Field(default=3, ge=1)
    news_max_new_per_source: int = Field(default=30, ge=1)
    news_max_summaries_per_run: int = Field(default=25, ge=0)
    news_max_summaries_per_run_local: int = Field(default=6, ge=0)
    news_max_attempts: int = Field(default=3, ge=1)
    news_fetch_full_text: bool = True
    news_retention_days: int = Field(default=30, ge=1)
    # Personal layer
    news_breaking_importance: int = Field(default=8, ge=1, le=10)
    news_breaking_max_age_hours: int = Field(default=12, ge=1)
    news_briefing_auto: bool = True
    news_briefing_stale_hours: float = Field(default=4.0, ge=0.5)
    news_briefing_size: int = Field(default=6, ge=3, le=12)

    # X (Twitter) accounts as sources, read for free (see modules/news/xsource.py). Each new
    # post costs one AI summary, so this caps how many are taken per account per check.
    x_refresh_minutes: int = Field(default=30, ge=10)
    x_max_posts_per_check: int = Field(default=5, ge=1, le=20)
    # Optional: your own Nitter server (e.g. http://127.0.0.1:8788). Empty = use FxTwitter.
    x_nitter_url: str = ""

    # --- Tasks module ---------------------------------------------------------------
    tasks_reminder_check_s: int = Field(default=20, ge=5)

    # --- Today ------------------------------------------------------------------------
    # When to announce the day's brief on open pages ("HH:MM", your time). Empty = never.
    today_brief_time: str = "08:00"

    @field_validator("today_brief_time")
    @classmethod
    def _valid_brief_time(cls, v: str) -> str:
        v = v.strip()
        if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("today_brief_time must look like 08:00 (or be empty)")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        v = v.strip()
        if v:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError(f"Unknown time zone '{v}' (use a name like Asia/Dhaka)") from e
        return v

    @field_validator("llm_providers")
    @classmethod
    def _valid_order(cls, v: str) -> str:
        return ",".join(_parse_order(v))

    @field_validator("news_llm_provider")
    @classmethod
    def _valid_news_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("auto", "none") and v not in PROVIDERS:
            raise ValueError(
                f"news_llm_provider must be auto, none or one of {', '.join(PROVIDERS)}"
            )
        return v

    # --- paths --------------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / "neurovex.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def check_bind_host(self) -> None:
        """Refuse to bind to a non-loopback address unless explicitly allowed."""
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.allow_remote:
            raise ValueError(
                f"Refusing to bind to {self.host!r}: Neurovex holds personal data. "
                "Set NEUROVEX_ALLOW_REMOTE=true if you really mean it."
            )

    # --- LLM helpers -------------------------------------------------------------------

    def llm_provider_order(self) -> list[str]:
        return _parse_order(self.llm_providers)

    def model_for(self, provider_id: str) -> str | None:
        if provider_id == "ollama":
            return self.ollama_model
        return getattr(self, f"{provider_id}_model", None)

    def llm_concurrency_for(self, provider_id: str) -> int:
        return self.llm_max_concurrency or PROVIDERS[provider_id].max_concurrency

    def secret(self, *names: str) -> str | None:
        """First non-empty value for any of `names` (see module docstring for order)."""
        for name in names:
            if not name:
                continue
            for env_name in (name, f"NEUROVEX_{name}"):
                if value := os.environ.get(env_name, "").strip():
                    return value
            if self.profile is Profile.TEST:
                continue  # tests never read real keys from files or the keychain
            if value := _dotenv().get(name) or _dotenv().get(f"NEUROVEX_{name}"):
                return value.strip()
            if self.use_keyring and (value := get_secret(name)):
                return value
        return None


@lru_cache
def _dotenv() -> dict[str, str]:
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(ENV_FILE).items() if v}
    except Exception:
        return {}


def get_secret(name: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # keyring backend missing or locked (e.g. headless Linux)
        return None


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
