"""What chat may see and change, per module (Settings → "What chat may do").

Each module declares permissions like `tasks.read` and `tasks.write`. A skill needs the
permission matching its module and risk (or the ones it lists itself), and the check happens
in `SkillRegistry.execute`, the one place every skill runs. Memory also reaches the AI outside
skills (recall into the prompt, changes riding on replies), so the chat engine checks
`memory.read` and `memory.write` there too.

Neurovex's own modules are allowed everything on first start. Whatever you change is kept,
and a permission no module declares is never allowed.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.db import utcnow
from neurovex.security.models import PermissionGrant


class PermissionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self._granted: dict[str, bool] = {}
        self._declared: list[str] = []

    async def load(self, declared: list[str]) -> None:
        """Remember what modules declare; allow the new ones (built in), keep your choices."""
        self._declared = list(declared)
        async with self.sessions() as s:
            rows = {r.permission: r for r in await s.scalars(select(PermissionGrant))}
            for perm in declared:
                if perm not in rows:
                    rows[perm] = PermissionGrant(permission=perm, granted=True,
                                                 decided_by="built-in")  # fmt: skip
                    s.add(rows[perm])
            await s.commit()
        self._granted = {p: rows[p].granted for p in declared}

    def allowed(self, permission: str) -> bool:
        return self._granted.get(permission, False)

    def declared(self) -> list[str]:
        return list(self._declared)

    async def set(self, permission: str, granted: bool) -> None:
        if permission not in self._granted:
            raise KeyError(permission)
        async with self.sessions() as s:
            row = await s.get(PermissionGrant, permission)
            if row is None:
                row = PermissionGrant(permission=permission)
                s.add(row)
            row.granted, row.decided_by, row.updated_at = granted, "user", utcnow()
            await s.commit()
        self._granted[permission] = granted

    async def decided_by(self) -> dict[str, str]:
        async with self.sessions() as s:
            return {r.permission: r.decided_by for r in await s.scalars(select(PermissionGrant))}
