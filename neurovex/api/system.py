"""What Neurovex is made of and what chat may do: modules, skills, permissions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from neurovex.core.skills import permission_label

router = APIRouter(tags=["system"])


class PermissionIn(BaseModel):
    granted: bool


@router.get("/api/system/modules")
async def modules(request: Request) -> list[dict[str, object]]:
    """Each module and whether it's running or degraded (and why)."""
    return request.app.state.modules.status()


@router.get("/api/skills")
async def skills(request: Request) -> list[dict[str, object]]:
    """Every action chat can take, what it needs, and whether it's allowed now."""
    registry = request.app.state.chat.skills
    return [
        {"id": s.id, "module": s.group, "description": s.description, "risk": s.risk.value,
         "needs": s.needs, "allowed": not registry.denied(s), "params": s.schema()}
        for s in registry.list()
    ]  # fmt: skip


@router.get("/api/permissions")
async def permissions(request: Request) -> list[dict[str, object]]:
    store = request.app.state.permissions
    names = {m.id: m.name for m in request.app.state.modules.manifests()}
    who = await store.decided_by()
    out = []
    for perm in store.declared():
        module, _, kind = perm.partition(".")
        out.append({"permission": perm, "module": module, "module_name": names.get(module, module),
                    "kind": kind, "label": permission_label(perm), "granted": store.allowed(perm),
                    "decided_by": who.get(perm, "built-in")})  # fmt: skip
    return out


@router.post("/api/permissions/{permission}")
async def set_permission(request: Request, permission: str, body: PermissionIn) -> dict[str, bool]:
    store = request.app.state.permissions
    try:
        await store.set(permission, body.granted)
    except KeyError:
        raise HTTPException(404, f"No module uses the permission {permission}") from None
    action = "permission.allow" if body.granted else "permission.deny"
    await request.app.state.audit.append("user", action, {"permission": permission})
    return {"granted": body.granted}
