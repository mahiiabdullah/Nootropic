"""Your day at a glance: /api/today."""

from __future__ import annotations

from fastapi import APIRouter, Request

from neurovex.today.brief import TodayOut

router = APIRouter(prefix="/api/today", tags=["today"])


@router.get("", response_model=TodayOut)
async def today(request: Request) -> TodayOut:
    return await request.app.state.today.brief.build()
