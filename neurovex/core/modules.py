"""Modules: what each part of Neurovex is, what it may do, and starting it safely.

Every module declares a `ModuleManifest`: its id, name, the permissions its skills use
(`news.read`, `tasks.write`, …) and the modules it depends on. The `ModuleManager` starts
modules in dependency order. One that fails to start is marked `degraded`, and so is
anything depending on it, instead of taking the whole app down. See docs/ARCHITECTURE.md §7.

Manifests are Python constants rather than the YAML files §7 sketches: they're checked by
the type checker, and there's no extra dependency. Modules are listed in `main.create_app`
rather than discovered from disk, since they all ship with Neurovex.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from neurovex.core.logging import get_logger

log = get_logger(__name__)

State = Literal["waiting", "running", "degraded", "stopped"]


class ModuleManifest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)  # e.g. ["tasks.read", "tasks.write"]
    depends_on: list[str] = Field(default_factory=list)


@runtime_checkable
class Startable(Protocol):
    manifest: ModuleManifest

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass
class ModuleStatus:
    manifest: ModuleManifest
    module: Startable | None = None  # None: nothing to start (skills and permissions only)
    state: State = "waiting"
    error: str | None = None
    order: int = field(default=0)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.manifest.id,
            "name": self.manifest.name,
            "state": self.state,
            "error": self.error,
            "depends_on": self.manifest.depends_on,
        }


class ModuleManager:
    def __init__(self) -> None:
        self._modules: dict[str, ModuleStatus] = {}

    def add(self, module: Startable) -> None:
        """A module with a lifecycle (background loops, clients to close)."""
        self._register(ModuleStatus(module.manifest, module))

    def declare(self, manifest: ModuleManifest) -> None:
        """A module with nothing to start: it contributes skills and permissions only."""
        self._register(ModuleStatus(manifest))

    def _register(self, status: ModuleStatus) -> None:
        mid = status.manifest.id
        if mid in self._modules:
            raise ValueError(f"Module {mid} is already registered")
        status.order = len(self._modules)
        self._modules[mid] = status

    def manifests(self) -> list[ModuleManifest]:
        return [s.manifest for s in self._modules.values()]

    def permissions(self) -> list[str]:
        """Every permission any module declares, in a stable order."""
        return list(dict.fromkeys(p for m in self.manifests() for p in m.permissions))

    def start_order(self) -> list[str]:
        """Dependencies first; registration order otherwise. Raises on unknown deps or cycles."""
        done: list[str] = []
        visiting: set[str] = set()

        def visit(mid: str, path: tuple[str, ...]) -> None:
            if mid in done:
                return
            if mid in visiting:
                raise ValueError(f"Modules depend on each other in a loop: {' → '.join(path)}")
            if mid not in self._modules:
                raise ValueError(f"{path[-2]} depends on unknown module {mid}")
            visiting.add(mid)
            for dep in self._modules[mid].manifest.depends_on:
                visit(dep, (*path, dep))
            visiting.discard(mid)
            done.append(mid)

        for mid in sorted(self._modules, key=lambda m: self._modules[m].order):
            visit(mid, (mid,))
        return done

    async def start_all(self) -> None:
        for mid in self.start_order():
            status = self._modules[mid]
            broken = [d for d in status.manifest.depends_on
                      if self._modules[d].state == "degraded"]  # fmt: skip
            if broken:
                status.state, status.error = "degraded", f"needs {', '.join(broken)}"
                log.warning("module_degraded", module=mid, error=status.error)
                continue
            if status.module is None:
                status.state = "running"
                continue
            try:
                await status.module.start()
                status.state = "running"
            except Exception as e:  # one broken module must not take Neurovex down
                status.state, status.error = "degraded", f"{type(e).__name__}: {str(e)[:200]}"
                log.exception("module_start_failed", module=mid)

    async def stop_all(self) -> None:
        """Stop in reverse order. Degraded modules are stopped too: they may hold clients."""
        for mid in reversed(self.start_order()):
            status = self._modules[mid]
            if status.module is not None:
                try:
                    await status.module.stop()
                except Exception:
                    log.exception("module_stop_failed", module=mid)
            if status.state == "running":
                status.state = "stopped"

    def state(self, module_id: str) -> State | None:
        status = self._modules.get(module_id)
        return status.state if status else None

    def status(self) -> list[dict[str, object]]:
        return [self._modules[m].to_dict() for m in self.start_order()]
