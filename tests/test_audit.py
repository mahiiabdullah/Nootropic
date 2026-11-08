"""The audit log: a hash chain that names the first tampered entry; skills are recorded."""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import select, update

from neurovex.core.skills import Risk, Skill, SkillRegistry, SkillResult
from neurovex.security.audit import AuditLog, params_hash
from neurovex.security.models import AuditEntry


async def test_the_chain_verifies_and_tampering_is_found_where_it_happened(sessions):
    log = AuditLog(sessions)
    for i in range(5):
        await log.append("chat", f"news.search.{i}", {"query": f"q{i}"})
    verdict = await log.verify()
    assert verdict.ok and verdict.entries == 5

    async with sessions() as s:
        third = (await s.scalars(select(AuditEntry).order_by(AuditEntry.id))).all()[2]
        await s.execute(update(AuditEntry).where(AuditEntry.id == third.id).values(outcome="ok!"))
        await s.commit()
    verdict = await log.verify()
    assert not verdict.ok and verdict.first_bad_id == third.id


async def test_deleting_an_entry_breaks_the_chain(sessions):
    log = AuditLog(sessions)
    for _ in range(3):
        await log.append("user", "auth.login", outcome="ok")
    async with sessions() as s:
        rows = (await s.scalars(select(AuditEntry).order_by(AuditEntry.id))).all()
        await s.delete(rows[1])
        await s.commit()
    verdict = await log.verify()
    assert not verdict.ok and verdict.first_bad_id == rows[2].id


class Q(BaseModel):
    query: str


async def test_every_skill_call_is_recorded_without_its_arguments(sessions):
    log = AuditLog(sessions)
    reg = SkillRegistry(audit=log.append)

    async def search(p: Q) -> SkillResult:
        return SkillResult(ok=True, summary=f"found {p.query}")

    async def wipe(_p: BaseModel) -> SkillResult:
        return SkillResult(ok=True, summary="wiped")

    async def broken(_p: BaseModel) -> SkillResult:
        raise RuntimeError("boom")

    reg.register(Skill("news.search", "Search", search, Q))
    reg.register(Skill("memory.clear", "Clear", wipe, risk=Risk.DESTRUCTIVE))
    reg.register(Skill("x.broken", "Broken", broken))
    secret_ask = {"query": "my private diary entry"}
    await reg.execute("news.search", secret_ask)
    await reg.execute("memory.clear", {})
    await reg.execute("memory.clear", {}, confirmed=True)
    await reg.execute("news.search", {"wrong": 1})
    await reg.execute("x.broken", {})
    await reg.execute("nope", {})

    rows = await log.tail(10)
    assert [(r.action, r.outcome, r.confirmed) for r in rows] == [
        ("news.search", "ok", False),
        ("memory.clear", "needs_confirmation", False),
        ("memory.clear", "ok", True),
        ("news.search", "invalid", False),
        ("x.broken", "error", False),
        ("nope", "unknown_skill", False),
    ]
    assert rows[0].params_hash == params_hash(secret_ask)
    async with sessions() as s:
        dump = " ".join(
            str(v) for r in await s.scalars(select(AuditEntry)) for v in vars(r).values()
        )
    assert "diary" not in dump
    assert (await log.verify()).ok


async def test_a_failing_audit_never_breaks_the_action(sessions, caplog):
    def broken_sessions():
        raise OSError("disk full")

    log = AuditLog(broken_sessions)  # type: ignore[arg-type]
    reg = SkillRegistry(audit=log.append)

    async def ping(_p: BaseModel) -> SkillResult:
        return SkillResult(ok=True, summary="pong")

    reg.register(Skill("x.ping", "Ping", ping))
    with caplog.at_level(logging.WARNING):
        assert (await reg.execute("x.ping", {})).ok
