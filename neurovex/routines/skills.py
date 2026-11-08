"""Chat skills for routines: teach one, list them, delete one. Running needs no skill: saying
a trigger runs the routine before any AI is asked (the fast path in the chat engine)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from neurovex.core.modules import ModuleManifest
from neurovex.core.skills import Risk, Skill, SkillRegistry, SkillResult
from neurovex.routines.store import RoutineError, RoutineStore, render

MANIFEST = ModuleManifest(
    id="routines",
    name="Routines",
    description="Things you teach Neurovex to do when you say a phrase.",
    permissions=["routines.read", "routines.write"],
)

ROUTINE_HINTS = r"\b(routines?|when(ever)? i say|teach you|shortcut)\b"


class RoutineStep(BaseModel):
    tool: str = Field(description="a tool id from the list, e.g. tasks.next")
    args: dict[str, Any] = Field(default_factory=dict)


class SaveParams(BaseModel):
    name: str = Field(description="short name, e.g. 'Start my day'")
    triggers: list[str] = Field(description="what the user will say, e.g. ['start my day']")
    steps: list[RoutineStep] = Field(
        description='e.g. [{"tool":"tasks.next","args":{}}]; in args {{input}} = words said '
        "after the phrase, {{date}} = today"
    )


class RefParams(BaseModel):
    routine: str = Field(description="name or id")


def check_steps(registry: SkillRegistry, steps: list[RoutineStep]) -> list[dict[str, Any]]:
    """Every step must be a real tool with arguments it accepts. Raises RoutineError."""
    sample = datetime.now(UTC)
    out = []
    for n, st in enumerate(steps, 1):
        skill = registry.get(st.tool)
        if skill is None or st.tool.startswith("routines."):
            raise RoutineError(f"Step {n}: '{st.tool}' isn't a tool a routine can run.")
        filled = render(st.args, "sample words", sample)
        try:
            skill.params.model_validate(filled)
        except ValidationError as e:
            problem = e.errors()[0]
            where = ".".join(str(p) for p in problem["loc"]) or "arguments"
            raise RoutineError(f"Step {n} ({st.tool}): {where}: {problem['msg']}") from None
        out.append({"tool": st.tool, "args": st.args})
    return out


def describe(steps: list[dict[str, Any]]) -> str:
    return " → ".join(
        st["tool"] + (f"({json.dumps(st['args'], ensure_ascii=False)})" if st["args"] else "")
        for st in steps
    )


def routine_skills(store: RoutineStore, registry: SkillRegistry) -> list[Skill]:
    async def save(p: SaveParams) -> SkillResult:
        try:
            steps = check_steps(registry, p.steps)
            routine, created = await store.save(p.name, p.triggers, steps)
        except RoutineError as e:
            return SkillResult.fail(str(e))
        verb = "Saved" if created else "Updated"
        say = " or ".join(f"'{t}'" for t in routine.triggers)
        return SkillResult(
            ok=True,
            summary=f"{verb} routine '{routine.name}': say {say} to run {describe(steps)}",
            data={"id": routine.id, "name": routine.name, "triggers": routine.triggers},
        )

    async def list_all(_p: BaseModel) -> SkillResult:
        routines = await store.list()
        items = [{"id": r.id, "name": r.name, "say": r.triggers, "steps": describe(r.steps)}
                 for r in routines]  # fmt: skip
        return SkillResult(ok=True, summary=f"{len(items)} routines", data=items)

    async def delete(p: RefParams) -> SkillResult:
        found = await store.find(p.routine)
        if found is None or await store.delete(found.id) is None:
            return SkillResult.fail(f"No routine called '{p.routine}'.")
        return SkillResult(ok=True, summary=f"Deleted the routine '{found.name}'.")

    return [
        Skill(
            "routines.save",
            "Teach a routine: when the user says a phrase, run these tools in "
            "order. Use when they say 'when I say X, do Y'. Don't also do the steps now.",
            save,
            SaveParams,
            risk=Risk.LOCAL_WRITE,
        ),  # fmt: skip
        Skill("routines.list", "The routines the user has taught, with their phrases.", list_all),
        Skill("routines.delete", "Delete a routine.", delete, RefParams, risk=Risk.LOCAL_WRITE),
    ]
