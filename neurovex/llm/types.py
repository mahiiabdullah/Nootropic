"""Request/result types shared by every provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neurovex.llm.ratelimit import RateInfo


@dataclass(frozen=True)
class JSONRequest:
    """Ask for one JSON object matching `schema`."""

    system: str
    user: str
    schema: dict[str, Any]
    schema_name: str = "result"
    max_tokens: int = 4000
    task: str = "general"  # for usage stats, e.g. "news.summarize"
    # The prompt itself spells out the reply format (chat): send no schema, just ask for a
    # JSON object. Saves the schema's tokens on every call; `parse` still validates.
    self_described: bool = False


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    json_mode: str | None = None
    attempts: list[str] = field(default_factory=list)  # "groq: rate_limited", ...
    rate: RateInfo | None = None  # rate-limit headers from the response, if any

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"
