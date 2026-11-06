"""Ask Neurovex: /api/chat."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from neurovex.conversation.engine import ChatEngine

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _engine(request: Request) -> ChatEngine:
    return request.app.state.chat


class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None


class TurnOut(BaseModel):
    conversation_id: int
    reply: str
    steps: list[dict[str, Any]]
    cards: list[dict[str, Any]]
    model: str | None
    error: bool
    memory: list[dict[str, Any]]


class ConversationOut(BaseModel):
    id: int
    title: str
    updated_at: datetime


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    steps: list[dict[str, Any]] | None
    cards: list[dict[str, Any]] | None
    model: str | None
    error: bool | None
    memory: list[dict[str, Any]] | None = None
    created_at: datetime


@router.post("", response_model=TurnOut)
async def ask(body: AskIn, request: Request) -> TurnOut:
    turn = await _engine(request).ask(body.message, body.conversation_id)
    return TurnOut(**turn.__dict__)


@router.get("/conversations", response_model=list[ConversationOut])
async def conversations(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[ConversationOut]:
    rows = await _engine(request).conversations(limit)
    return [ConversationOut(id=c.id, title=c.title, updated_at=c.updated_at) for c in rows]


@router.get("/conversations/{conversation_id}", response_model=list[MessageOut])
async def messages(conversation_id: int, request: Request) -> list[MessageOut]:
    rows = await _engine(request).messages(conversation_id)
    if rows is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            steps=m.steps,
            cards=m.cards,
            model=m.model,
            error=m.error,
            memory=m.memory_ops,
            created_at=m.created_at,
        )  # fmt: skip
        for m in rows
    ]


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(conversation_id: int, request: Request) -> None:
    if not await _engine(request).delete(conversation_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
