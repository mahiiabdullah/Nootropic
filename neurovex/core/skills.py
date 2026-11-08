"""Skills: the actions Neurovex can take, shared by chat, CLI and (later) voice.

A skill is one typed capability ("news.search", "news.follow") with Pydantic
parameters, a risk level and an async handler. Modules contribute skills; the
registry validates arguments, enforces risk rules and turns every outcome into a
`SkillResult` (never an exception), so a model can read what happened and recover.
See docs/ARCHITECTURE.md §6.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from neurovex.core.logging import get_logger

log = get_logger(__name__)


class Risk(StrEnum):
    READ = "read"  # look something up
    LOCAL_WRITE = "local_write"  # change Neurovex's own data (follow a topic, save a note)
    DESTRUCTIVE = "destructive"  # delete/overwrite: needs confirmation
    EXTERNAL = "external"  # acts outside Neurovex (send, submit, buy): needs confirmation
    EXEC = "exec"  # runs code: needs confirmation


NEEDS_CONFIRMATION = {Risk.DESTRUCTIVE, Risk.EXTERNAL, Risk.EXEC}
# The permission a skill needs by default: "<module>.<this>" (see security/permissions.py)
RISK_PERMISSION = {Risk.READ: "read", Risk.LOCAL_WRITE: "write", Risk.DESTRUCTIVE: "write",
                   Risk.EXTERNAL: "external", Risk.EXEC: "exec"}  # fmt: skip


def permission_label(permission: str) -> str:
    """'tasks.write' → 'changing tasks', for messages people read."""
    module, _, kind = permission.partition(".")
    verb = {"read": "seeing", "write": "changing"}.get(kind, kind)
    return f"{verb} {module}"


_SHORT_TYPES = {"string": "str", "integer": "int", "boolean": "bool", "number": "float",
                "array": "list"}  # fmt: skip


class NoParams(BaseModel):
    pass


def _explain(e: json.JSONDecodeError | ValidationError) -> str:
    """Short, model-readable reason: "repeat: Input should be 'daily' or 'weekly' (got 'x')"."""
    if isinstance(e, json.JSONDecodeError):
        return f"arguments_json is not valid JSON ({e.msg})"
    parts = []
    for err in e.errors()[:3]:
        where = ".".join(str(p) for p in err["loc"]) or "arguments"
        got = f" (got {json.dumps(err['input'], default=str)[:40]})" if "input" in err else ""
        parts.append(f"{where}: {err['msg']}{got}")
    return "; ".join(parts)


@dataclass
class SkillResult:
    ok: bool
    summary: str  # one line for people and logs: "Found 4 stories about Rust"
    data: Any = None  # what the model reads (kept small)
    display: dict[str, Any] | None = None  # optional card for the UI
    error: str | None = None

    @classmethod
    def fail(cls, error: str) -> SkillResult:
        return cls(ok=False, summary=error, error=error)


Handler = Callable[[BaseModel], Awaitable[SkillResult]]
# (actor, skill id, params, outcome, confirmed): the audit log's append
Recorder = Callable[..., Awaitable[None]]


@dataclass
class Skill:
    id: str
    description: str
    handler: Handler
    params: type[BaseModel] = NoParams
    risk: Risk = Risk.READ
    examples: list[str] = field(default_factory=list)
    # None: the module's permission for this risk ("tasks.write"); [] needs none.
    permissions: list[str] | None = None

    @property
    def group(self) -> str:
        """The module a skill belongs to: "news" for "news.search"."""
        return self.id.split(".", 1)[0]

    @property
    def needs(self) -> list[str]:
        if self.permissions is not None:
            return self.permissions
        return [f"{self.group}.{RISK_PERMISSION[self.risk]}"]

    def schema(self) -> dict[str, Any]:
        s = self.params.model_json_schema()
        return {"properties": s.get("properties", {}), "required": s.get("required", [])}

    def signature(self) -> str:
        """Compact signature for prompts, e.g. `news.search(query?: str, hours: int = 168)`.

        Several times smaller than raw JSON Schema, which matters because the tool list
        is resent on every chat step. `name?` = optional with an empty default.
        """
        schema = self.schema()
        required = set(schema["required"])
        parts = []
        for name, p in schema["properties"].items():
            options = [p, *p.get("anyOf", [])]  # Optional[...] fields nest their type in anyOf
            if enum := next((x["enum"] for x in options if "enum" in x), None):
                kind = "|".join(str(v) for v in enum if v != "")
            else:
                kind = p.get("type") or next(
                    (x.get("type") for x in options[1:] if x.get("type") != "null"), "any"
                )
                kind = _SHORT_TYPES.get(kind, kind)
            default = p.get("default")
            if name in required:
                part = f"{name}: {kind}"
            elif default in (None, "", []):
                part = f"{name}?: {kind}"
            else:
                part = f"{name}: {kind} = {json.dumps(default)}"
            if desc := p.get("description"):
                part += f" ({desc[:140]})"
            parts.append(part)
        return f"{self.id}({', '.join(parts)})"


class SkillRegistry:
    def __init__(
        self, audit: Recorder | None = None, allowed: Callable[[str], bool] | None = None
    ) -> None:
        self._skills: dict[str, Skill] = {}
        self._hints: dict[str, re.Pattern[str]] = {}
        self._audit = audit
        self._allowed = allowed  # None: everything is allowed (tests, tools)

    def denied(self, skill: Skill) -> list[str]:
        """The permissions this skill needs that the user has turned off."""
        if self._allowed is None:
            return []
        return [p for p in skill.needs if not self._allowed(p)]

    def register(self, skill: Skill) -> None:
        if skill.id in self._skills:
            raise ValueError(f"Skill {skill.id} is already registered")
        self._skills[skill.id] = skill

    def register_all(self, skills: Iterable[Skill]) -> None:
        for s in skills:
            self.register(s)

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def hint(self, group: str, pattern: str) -> None:
        """Words suggesting a request needs `group`'s tools. Groups without hints (small,
        general ones like memory) are always described in full."""
        self._hints[group] = re.compile(pattern, re.IGNORECASE)

    def groups_for(self, text: str, sticky: Iterable[str] = ()) -> set[str] | None:
        """Tool groups a request likely needs, or None (= all) when nothing points anywhere.
        `sticky`: groups the previous answer used, so follow-ups keep their tools."""
        matched = {g for g, pattern in self._hints.items() if pattern.search(text)}
        matched |= set(sticky) & self._hints.keys()
        if not matched:
            return None
        always = {s.group for s in self._skills.values() if s.group not in self._hints}
        return matched | always

    def describe(self, groups: Collection[str] | None = None) -> str:
        """Compact tool catalog for a model prompt: one signature line per skill, for the
        given groups (default all); other tools are listed by id and can still be used."""
        shown = [s for s in self._skills.values() if groups is None or s.group in groups]
        lines = [f"- {s.signature()}: {s.description}" for s in shown]
        if hidden := [s.id for s in self._skills.values() if s not in shown]:
            lines.append(f"- Also available (arguments are checked): {', '.join(hidden)}")
        return "\n".join(lines)

    async def execute(
        self,
        skill_id: str,
        args: dict[str, Any] | str | None,
        confirmed: bool = False,
        actor: str = "chat",
    ) -> SkillResult:
        """Run a skill. Every call, whatever its outcome, goes to the audit log."""
        result, outcome = await self._run(skill_id, args, confirmed)
        if self._audit is not None:
            await self._audit(actor, skill_id, args, outcome, confirmed)
        return result

    async def _run(
        self, skill_id: str, args: dict[str, Any] | str | None, confirmed: bool
    ) -> tuple[SkillResult, str]:
        skill = self._skills.get(skill_id)
        if skill is None:
            msg = f"Unknown tool '{skill_id}'. Use one of the listed tools."
            return SkillResult.fail(msg), "unknown_skill"
        if off := self.denied(skill):
            what = ", ".join(map(permission_label, off))
            msg = (f"The user has turned off {what} for chat (Settings → What chat may do). "
                   "Tell them, and don't try another way.")  # fmt: skip
            return SkillResult.fail(msg), "denied"
        if skill.risk in NEEDS_CONFIRMATION and not confirmed:
            msg = f"'{skill_id}' needs the user's confirmation first."
            return SkillResult.fail(msg), "needs_confirmation"
        try:
            raw = json.loads(args) if isinstance(args, str) and args.strip() else (args or {})
            params = skill.params.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as e:
            return SkillResult.fail(f"Invalid arguments for {skill_id}: {_explain(e)}"), "invalid"
        try:
            result = await skill.handler(params)
        except Exception as e:  # a broken skill must not break the conversation
            log.exception("skill_failed", skill=skill_id)
            msg = f"{skill_id} failed: {type(e).__name__}: {str(e)[:200]}"
            return SkillResult.fail(msg), "error"
        return result, "ok" if result.ok else "failed"
