"""Saving routines, recognizing their trigger phrases, and filling in their steps.

Matching is deliberately strict, so a routine never hijacks an ordinary question:
- the whole message is a trigger ("Start my day!"), or close to one (a typo or two), or
- the message starts with a trigger, and the rest becomes `{{input}}`
  ("log water 2 glasses" with the trigger "log water" → input "2 glasses").
"How do I start my day better?" runs nothing.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.db import utcnow
from neurovex.routines.models import Routine

MAX_STEPS = 8
MAX_TRIGGERS = 5
FUZZY = 0.88  # similarity for a whole-message match (catches a typo, not a different sentence)
_WORDS = re.compile(r"[a-z0-9']+")
_TEMPLATE = re.compile(r"\{\{\s*(input|date|time)\s*\}\}")


class RoutineError(ValueError):
    """Why a routine can't be saved, worded for the user (and the model)."""


def normalize(text: str) -> str:
    """'  Start my DAY, please! ' → 'start my day please'."""
    return " ".join(_WORDS.findall((text or "").lower().replace("’", "'")))


def render(value: Any, said: str, now: datetime) -> Any:
    """Fill {{input}}, {{date}} and {{time}} in a step's arguments (any depth)."""
    if isinstance(value, str):
        fills = {"input": said, "date": now.date().isoformat(), "time": now.strftime("%H:%M")}
        return _TEMPLATE.sub(lambda m: fills[m.group(1)], value).strip()
    if isinstance(value, dict):
        return {k: render(v, said, now) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, said, now) for v in value]
    return value


@dataclass
class Match:
    routine: Routine
    said: str  # the words after the trigger, for {{input}}
    trigger: str


def match_routine(routines: list[Routine], text: str) -> Match | None:
    """The routine this message runs, if any (see the module docstring for the rules)."""
    words = (text or "").split()
    norm = normalize(text)
    if not norm:
        return None
    best: tuple[float, Match] | None = None
    for r in routines:
        for trig in r.triggers:
            if norm == trig:
                return Match(r, "", trig)
            if norm.startswith(trig + " "):
                rest = " ".join(words[len(trig.split()) :]).strip(" ,:;-")
                score = 0.99
                candidate = Match(r, rest, trig)
            else:
                score = difflib.SequenceMatcher(None, norm, trig).ratio()
                candidate = Match(r, "", trig)
            if score >= FUZZY and (best is None or score > best[0]):
                best = (score, candidate)
    return best[1] if best else None


def clean_triggers(raw: list[str]) -> list[str]:
    triggers = list(dict.fromkeys(t for t in (normalize(x) for x in raw) if t))
    if not triggers:
        raise RoutineError("A routine needs at least one phrase to say, like 'start my day'.")
    if len(triggers) > MAX_TRIGGERS:
        raise RoutineError(f"Use at most {MAX_TRIGGERS} phrases for one routine.")
    for t in triggers:
        if len(t) < 3 or len(t) > 80:
            raise RoutineError(f"'{t}' is too short or too long to say as a trigger.")
    return triggers


class RoutineStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def list(self) -> list[Routine]:
        async with self.sessions() as s:
            return list(await s.scalars(select(Routine).order_by(Routine.name)))

    async def find(self, name_or_id: str | int) -> Routine | None:
        async with self.sessions() as s:
            if isinstance(name_or_id, int) or str(name_or_id).isdigit():
                return await s.get(Routine, int(name_or_id))
            want = normalize(str(name_or_id))
            for r in await s.scalars(select(Routine)):
                if normalize(r.name) == want or want in r.triggers:
                    return r
            return None

    async def match(self, text: str) -> Match | None:
        return match_routine(await self.list(), text)

    async def save(
        self, name: str, triggers: list[str], steps: list[dict[str, Any]]
    ) -> tuple[Routine, bool]:
        """Add a routine, or replace the one with this name. Returns (routine, created)."""
        name = " ".join((name or "").split())[:80]
        if not name:
            raise RoutineError("Give the routine a short name, like 'Start my day'.")
        if not steps:
            raise RoutineError("A routine needs at least one step.")
        if len(steps) > MAX_STEPS:
            raise RoutineError(f"Keep a routine to {MAX_STEPS} steps or fewer.")
        clean = clean_triggers(triggers)
        async with self.sessions() as s:
            existing = list(await s.scalars(select(Routine)))
            mine = next((r for r in existing if normalize(r.name) == normalize(name)), None)
            for other in existing:
                if other is mine:
                    continue
                if clash := set(clean) & set(other.triggers):
                    raise RoutineError(
                        f"'{sorted(clash)[0]}' already runs the routine '{other.name}'."
                    )
            if mine is None:
                mine = Routine(name=name, triggers=clean, steps=steps)
                s.add(mine)
                created = True
            else:
                mine.name, mine.triggers, mine.steps = name, clean, steps
                mine.updated_at = utcnow()
                created = False
            await s.commit()
            await s.refresh(mine)
            return mine, created

    async def delete(self, routine_id: int) -> Routine | None:
        async with self.sessions() as s:
            r = await s.get(Routine, routine_id)
            if r is None:
                return None
            await s.delete(r)
            await s.commit()
            return r

    async def mark_used(self, routine_id: int) -> None:
        async with self.sessions() as s:
            r = await s.get(Routine, routine_id)
            if r is not None:
                r.use_count += 1
                r.last_used_at = utcnow()
                await s.commit()
