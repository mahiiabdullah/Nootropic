"""Built-in skills that don't belong to a module."""

from __future__ import annotations

from neurovex.core.clock import local_now
from neurovex.core.skills import NoParams, Skill, SkillResult
from neurovex.llm.router import LLMRouter


def builtin_skills(router: LLMRouter, tz_name: str = "") -> list[Skill]:
    async def now(_: NoParams) -> SkillResult:
        local = local_now(tz_name)
        return SkillResult(
            ok=True,
            summary="Read the clock",
            data={"local": local.isoformat(timespec="minutes"), "weekday": local.strftime("%A")},
        )

    async def ai_status(_: NoParams) -> SkillResult:
        providers = [
            {
                "provider": st["name"],
                "status": st["status"],
                "model": st["model"],
                "next_free_slot_s": st["rate"]["next_slot_s"],
                "last_error": st["last_error"],
            }
            for st in router.status()
        ]
        return SkillResult(ok=True, summary="Checked AI providers", data=providers)

    return [
        Skill("system.time", "Current local date and time.", now, permissions=[]),
        Skill(
            "system.ai_status",
            "Health and rate budget of Neurovex's AI providers.",
            ai_status,
            permissions=[],
        ),  # fmt: skip
    ]
