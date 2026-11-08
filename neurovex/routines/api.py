"""Routines on the web page: list them and delete one. They're taught in Ask, and running one
is just saying its phrase there."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from neurovex.routines.skills import describe

router = APIRouter(prefix="/api/routines", tags=["routines"])


@router.get("")
async def list_routines(request: Request) -> list[dict[str, object]]:
    store = request.app.state.chat.routines
    return [
        {"id": r.id, "name": r.name, "triggers": r.triggers, "steps": describe(r.steps),
         "use_count": r.use_count,
         "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None}
        for r in await store.list()
    ]  # fmt: skip


@router.delete("/{routine_id}")
async def delete_routine(request: Request, routine_id: int) -> dict[str, bool]:
    if await request.app.state.chat.routines.delete(routine_id) is None:
        raise HTTPException(404, "No such routine")
    await request.app.state.audit.append("user", "routines.delete", {"id": routine_id})
    return {"deleted": True}
