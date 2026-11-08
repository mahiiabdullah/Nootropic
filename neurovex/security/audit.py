"""A tamper-evident audit log (`nvx audit tail | verify`).

Every skill run (anything chat does) and every sign-in event is appended as one entry whose
hash covers the entry before it, so editing or deleting a row breaks the chain from there on,
and `verify` names the first broken entry. Parameters are stored only as a hash: the log says
what was done and when, without copying what you asked into another table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.db import utcnow
from neurovex.core.logging import get_logger
from neurovex.security.models import AuditEntry

log = get_logger(__name__)

GENESIS = "0" * 64


def params_hash(params: Any) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def entry_hash(
    prev: str, at: datetime, actor: str, action: str, p_hash: str, outcome: str, confirmed: bool
) -> str:
    stamp = at.astimezone(UTC).isoformat(timespec="microseconds")
    fields = [prev, stamp, actor, action, p_hash, outcome, "1" if confirmed else "0"]
    return hashlib.sha256("|".join(fields).encode()).hexdigest()


@dataclass
class Verdict:
    ok: bool
    entries: int
    first_bad_id: int | None = None


class AuditLog:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self._lock = asyncio.Lock()

    async def append(
        self,
        actor: str,
        action: str,
        params: Any = None,
        outcome: str = "ok",
        confirmed: bool = False,
    ) -> None:
        """Add an entry. Never raises: a full disk must not stop what's being recorded."""
        try:
            async with self._lock, self.sessions() as s:
                last = await s.scalar(select(AuditEntry.hash).order_by(AuditEntry.id.desc()))
                prev = last or GENESIS
                at = utcnow()
                p_hash = params_hash(params)
                outcome = outcome[:40]
                s.add(
                    AuditEntry(
                        at=at, actor=actor, action=action[:80], params_hash=p_hash,
                        outcome=outcome, confirmed=confirmed, prev_hash=prev,
                        hash=entry_hash(prev, at, actor, action[:80], p_hash, outcome, confirmed),
                    )
                )  # fmt: skip
                await s.commit()
        except Exception as e:
            log.warning("audit_append_failed", action=action, error=type(e).__name__)

    async def tail(self, limit: int = 20) -> list[AuditEntry]:
        async with self.sessions() as s:
            rows = await s.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit))
            return list(reversed(list(rows)))

    async def verify(self) -> Verdict:
        """Walk the whole chain; report the first entry that doesn't match."""
        prev, count = GENESIS, 0
        async with self.sessions() as s:
            for row in await s.scalars(select(AuditEntry).order_by(AuditEntry.id)):
                count += 1
                expected = entry_hash(
                    prev, row.at, row.actor, row.action, row.params_hash, row.outcome, row.confirmed
                )
                if row.prev_hash != prev or row.hash != expected:
                    return Verdict(ok=False, entries=count, first_bad_id=row.id)
                prev = row.hash
        return Verdict(ok=True, entries=count)
