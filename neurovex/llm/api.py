"""AI provider status, tests and model lists: /api/llm."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from neurovex.llm.errors import LLMError, ProvidersExhausted
from neurovex.llm.router import LLMRouter
from neurovex.llm.usage import usage_summary

router = APIRouter(prefix="/api/llm", tags=["llm"])


def _llm(request: Request) -> LLMRouter:
    return request.app.state.llm


@router.get("/providers")
async def providers(
    request: Request, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24
) -> dict[str, Any]:
    """Health of every provider in the chain plus persisted usage for the last `hours`."""
    llm = _llm(request)
    llm.refresh()  # pick up keys added since startup
    usage: list[dict] = []
    news = getattr(request.app.state, "news", None)
    if news is not None:
        async with news.sessions() as s:
            usage = await usage_summary(s, hours)
    return {
        "strategy": llm.settings.llm_strategy,
        "order": llm.order,
        "active": llm.label(),
        "providers": llm.status(),
        "usage": usage,
        "usage_hours": hours,
    }


@router.post("/test")
async def test(request: Request, provider: str | None = None) -> list[dict[str, Any]]:
    """Send a tiny JSON request to one provider (or all configured ones)."""
    llm = _llm(request)
    llm.refresh()
    if provider:
        if provider not in llm.states:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"{provider} is not in the provider list"
            )
        return [await llm.test(provider)]
    return await llm.test_all()


@router.get("/models/{provider}")
async def models(provider: str, request: Request) -> list[str]:
    llm = _llm(request)
    llm.refresh()
    try:
        return await llm.list_models(provider)
    except ProvidersExhausted as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except LLMError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
