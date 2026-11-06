"""Persistent usage log of every LLM attempt (table `llm_calls`), and summaries of it."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Integer, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from neurovex.core.db import Base, UTCDateTime, utcnow
from neurovex.llm.router import CallRecord, Recorder

RETENTION_DAYS = 30


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(200))
    task: Mapped[str] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(30))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


def db_recorder(sessions: async_sessionmaker[AsyncSession]) -> Recorder:
    writes = 0

    async def record(rec: CallRecord) -> None:
        nonlocal writes
        async with sessions() as s:
            s.add(
                LLMCall(
                    at=rec.at,
                    provider=rec.provider,
                    model=rec.model,
                    task=rec.task,
                    outcome=rec.outcome,
                    latency_ms=rec.latency_ms,
                    input_tokens=rec.input_tokens,
                    output_tokens=rec.output_tokens,
                    error=rec.error,
                )  # fmt: skip
            )
            writes += 1
            if writes % 500 == 1:  # occasional housekeeping
                await s.execute(
                    delete(LLMCall).where(LLMCall.at < utcnow() - timedelta(days=RETENTION_DAYS))
                )
            await s.commit()

    return record


async def usage_summary(session: AsyncSession, hours: int = 24) -> list[dict]:
    """Per provider: attempts, successes, failures by kind, tokens, avg latency."""
    since = utcnow() - timedelta(hours=hours)
    rows = await session.execute(
        select(
            LLMCall.provider,
            LLMCall.outcome,
            func.count(),
            func.sum(LLMCall.input_tokens),
            func.sum(LLMCall.output_tokens),
            func.avg(LLMCall.latency_ms),
        )
        .where(LLMCall.at >= since)
        .group_by(LLMCall.provider, LLMCall.outcome)
    )
    out: dict[str, dict] = {}
    for provider, outcome, n, tin, tout, lat in rows.all():
        p = out.setdefault(
            provider,
            {"provider": provider, "attempts": 0, "ok": 0, "failures": {}, "input_tokens": 0,
             "output_tokens": 0, "avg_latency_ms": None},
        )  # fmt: skip
        p["attempts"] += n
        if outcome == "ok":
            p["ok"] = n
            p["avg_latency_ms"] = round(lat or 0)
            p["input_tokens"] += tin or 0
            p["output_tokens"] += tout or 0
        else:
            p["failures"][outcome] = n
    return sorted(out.values(), key=lambda p: -p["attempts"])
