"""Memory skills for chat. Writing happens through the chat step's `memory` field;
these are for looking things up beyond the small block shown each turn."""

from __future__ import annotations

from pydantic import BaseModel, Field

from neurovex.core.skills import Skill, SkillResult
from neurovex.memory.models import KINDS
from neurovex.memory.store import MemoryStore


class SearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    include_conversations: bool = Field(default=True, description="Also search past chats")


class ListParams(BaseModel):
    kind: str | None = Field(default=None, description=f"One of {', '.join(KINDS)}")


def memory_skills(store: MemoryStore) -> list[Skill]:
    async def search(p: SearchParams) -> SkillResult:
        memories = await store.search(p.query, limit=10)
        convs = (
            await store.search_conversations(p.query, limit=6) if p.include_conversations else []
        )
        data = {
            "memories": [{"id": m.id, "kind": m.kind, "text": m.text} for m in memories],
            "past_conversations": [
                {
                    "when": h.at.date().isoformat(),
                    "conversation": h.title,
                    "who": h.role,
                    "snippet": h.snippet,
                }  # fmt: skip
                for h in convs
            ],
        }
        return SkillResult(
            ok=True,
            summary=f"{len(memories)} memories, {len(convs)} chat matches for '{p.query}'",
            data=data,
        )

    async def list_all(p: ListParams) -> SkillResult:
        kind = p.kind if p.kind in KINDS else None
        items = await store.list(kind=kind, limit=60)
        return SkillResult(
            ok=True,
            summary=f"{len(items)} memories" + (f" ({kind})" if kind else ""),
            data=[{"id": m.id, "kind": m.kind, "text": m.text, "pinned": m.pinned} for m in items],
        )

    return [
        Skill(
            "memory.search",
            "Search what you remember about the user and past conversations.",
            search,
            SearchParams,
        ),  # fmt: skip
        Skill(
            "memory.list",
            "List everything you remember about the user (optionally one kind).",
            list_all,
            ListParams,
        ),  # fmt: skip
    ]
