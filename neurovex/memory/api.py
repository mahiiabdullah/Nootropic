"""Your memory, under your control: /api/memory."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from neurovex.memory.models import KINDS, Memory
from neurovex.memory.store import MemoryStore

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _store(request: Request) -> MemoryStore:
    return request.app.state.memory


class MemoryOut(BaseModel):
    id: int
    kind: str
    text: str
    importance: int
    pinned: bool
    source: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    use_count: int


class MemoryIn(BaseModel):
    text: str = Field(min_length=3, max_length=300)
    kind: str = "fact"
    importance: int = Field(default=2, ge=1, le=3)
    pinned: bool = False


class MemoryPatch(BaseModel):
    text: str | None = Field(default=None, min_length=3, max_length=300)
    kind: str | None = None
    importance: int | None = Field(default=None, ge=1, le=3)
    pinned: bool | None = None


class ConversationHitOut(BaseModel):
    conversation_id: int
    title: str
    role: str
    snippet: str
    at: datetime


class SearchOut(BaseModel):
    memories: list[MemoryOut]
    conversations: list[ConversationHitOut]


def _out(m: Memory) -> MemoryOut:
    return MemoryOut.model_validate(m, from_attributes=True)


def _check_kind(kind: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"kind must be one of {', '.join(KINDS)}"
        )


@router.get("", response_model=list[MemoryOut])
async def list_memories(request: Request, kind: str | None = None) -> list[MemoryOut]:
    _check_kind(kind)
    return [_out(m) for m in await _store(request).list(kind=kind)]


@router.get("/search", response_model=SearchOut)
async def search(
    request: Request, q: Annotated[str, Query(min_length=1, max_length=200)]
) -> SearchOut:
    store = _store(request)
    return SearchOut(
        memories=[_out(m) for m in await store.search(q, limit=20)],
        conversations=[
            ConversationHitOut(**h.__dict__) for h in await store.search_conversations(q, limit=10)
        ],
    )


@router.post("", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def add(body: MemoryIn, request: Request) -> MemoryOut:
    _check_kind(body.kind)
    m, how = await _store(request).add(
        body.text, body.kind, body.importance, pinned=body.pinned, source="user"
    )
    if m is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Not stored: {how}")
    return _out(m)


@router.patch("/{memory_id}", response_model=MemoryOut)
async def edit(memory_id: int, body: MemoryPatch, request: Request) -> MemoryOut:
    _check_kind(body.kind)
    store = _store(request)
    if await store.get(memory_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")
    m = await store.update(
        memory_id, text_=body.text, kind=body.kind, importance=body.importance, pinned=body.pinned
    )
    if m is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Text rejected (empty or a secret)"
        )
    return _out(m)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget(memory_id: int, request: Request) -> None:
    if await _store(request).forget(memory_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")


@router.delete("", status_code=status.HTTP_200_OK)
async def forget_everything(
    request: Request, confirm: Annotated[str, Query(description="must be 'forget-everything'")]
) -> dict[str, int]:
    if confirm != "forget-everything":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass confirm=forget-everything")
    return {"forgotten": await _store(request).clear()}
