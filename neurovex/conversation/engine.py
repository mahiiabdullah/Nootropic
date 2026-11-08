"""Conversation engine: answer the user, using skills when they help.

Providers expose tool calling differently (or not at all), so Neurovex uses one
provider-neutral protocol on top of `LLMRouter.complete_json`: each step the model
returns a small JSON object, either "use this tool with these arguments" or "respond
with this message". Tool results are fed back as fenced data, up to MAX_TOOL_STEPS
times. This keeps chat working on any provider in the chain, with the same pacing
and failover as everything else.

Memory rides along for free: before each turn a small `<memory>` block is recalled
locally (SQLite full-text search, no AI call), and every step may carry `memory`
operations (add / update / forget) that are applied as they arrive, so remembering
never costs an extra model call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.conversation.models import ChatConversation, ChatMessage
from neurovex.core.clock import local_now
from neurovex.core.db import utcnow
from neurovex.core.logging import get_logger
from neurovex.core.skills import Risk, SkillRegistry, SkillResult
from neurovex.llm.errors import ProvidersExhausted
from neurovex.llm.router import LLMRouter
from neurovex.llm.types import JSONRequest
from neurovex.memory.models import KINDS
from neurovex.memory.recall import recall
from neurovex.memory.store import MemoryStore
from neurovex.routines.store import Match, RoutineStore, render

log = get_logger(__name__)

MAX_TOOL_STEPS = 4
HISTORY_MESSAGES = 12
TOOL_RESULT_CHARS = 3_000
CHAT_MAX_WAIT_S = 20.0  # interactive: wait a little for rate budget, not minutes
# Some providers charge max_tokens up front: steps use ~80 tokens (p90 ~190, max seen 240).
STEP_MAX_TOKENS = 500
MAX_MEMORY_OPS = 5  # per step
MAX_EXTRA_CALLS = 3  # `then`: more tool calls in the same step
HISTORY_REPLY_CHARS = 600  # older answers are context, not something to re-read in full

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["respond", "use_tool"]},
        "tool": {"type": "string"},
        "arguments_json": {"type": "string"},
        "message": {"type": "string"},
        "then": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tool": {"type": "string"}, "arguments_json": {"type": "string"}},
                "required": ["tool", "arguments_json"],
                "additionalProperties": False,
            },
        },
        "memory": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add", "update", "forget"]},
                    "id": {"type": "integer"},
                    "kind": {"type": "string", "enum": KINDS},
                    "text": {"type": "string"},
                    "importance": {"type": "integer"},
                },
                "required": ["op", "id", "kind", "text", "importance"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action", "tool", "arguments_json", "message", "then", "memory"],
    "additionalProperties": False,
}


class MemoryOp(BaseModel):
    op: Literal["add", "update", "forget"]
    id: int = 0
    kind: str = "fact"
    text: str = ""
    importance: int = 2


class Step(BaseModel):
    action: Literal["respond", "use_tool"]
    tool: str = ""
    arguments_json: str = ""
    message: str = ""
    # Raw dicts: one malformed entry must not invalidate the whole step.
    then: list[dict[str, Any]] = []
    memory: list[dict[str, Any]] = []

    def calls(self) -> list[tuple[str, str]]:
        """(tool, arguments_json) for every call in this step, in order."""
        out = [(self.tool.strip(), self.arguments_json)]
        for extra in self.then[:MAX_EXTRA_CALLS]:
            if isinstance(extra, dict) and str(extra.get("tool", "")).strip():
                out.append((str(extra["tool"]).strip(), str(extra.get("arguments_json") or "")))
        return out

    def memory_ops(self) -> list[MemoryOp]:
        ops = []
        for raw in self.memory[:MAX_MEMORY_OPS]:
            try:
                ops.append(MemoryOp.model_validate(raw))
            except ValueError:
                continue
        return ops

    @model_validator(mode="after")
    def _complete(self) -> Step:
        if self.action == "respond" and not self.message.strip():
            raise ValueError("respond needs a message")
        if self.action == "use_tool" and not self.tool.strip():
            raise ValueError("use_tool needs a tool id")
        return self


def parse_step(raw: str) -> Step:
    return Step.model_validate_json(raw)


@dataclass
class ChatTurn:
    conversation_id: int
    reply: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    error: bool = False
    memory: list[dict[str, Any]] = field(default_factory=list)  # what was remembered/forgotten


@cache
def prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def _local_now(tz_name: str = "") -> str:
    now = local_now(tz_name)
    z = now.strftime("%z")  # "+0600"; Windows' %Z gives long names like "Bangladesh Standard Time"
    return now.strftime("%A %d %B %Y, %H:%M") + f" (UTC{z[:3]}:{z[3:]})"


def _fence(tool: str, args: str, result: SkillResult) -> str:
    payload = {"ok": result.ok, "summary": result.summary}
    if result.ok:
        payload["data"] = result.data
    else:
        payload["error"] = result.error
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > TOOL_RESULT_CHARS:
        text = text[:TOOL_RESULT_CHARS] + "…(truncated)"
    safe_args = args.replace('"', "'")[:200]
    return f'<tool_result tool="{tool}" args="{safe_args}">\n{text}\n</tool_result>'


class ChatEngine:
    def __init__(
        self,
        router: LLMRouter,
        skills: SkillRegistry,
        sessions: async_sessionmaker[AsyncSession],
        memory: MemoryStore | None = None,
        tz_name: str = "",
        allowed: Callable[[str], bool] | None = None,
        routines: RoutineStore | None = None,
    ) -> None:
        self.router = router
        self.skills = skills
        self.sessions = sessions
        self.memory = memory
        self.tz_name = tz_name
        self._allowed = allowed  # Settings → What chat may do (None: everything)
        self.routines = routines

    def may(self, permission: str) -> bool:
        return self._allowed is None or self._allowed(permission)

    def system_prompt(self, groups: set[str] | None = None) -> str:
        """Persona + protocol. `groups` limits full tool signatures to those groups (the
        rest are listed by id), which keeps every step of a focused request cheaper."""
        protocol = prompt("protocol").format(
            tools=self.skills.describe(groups),
            max_steps=MAX_TOOL_STEPS,
            now=_local_now(self.tz_name),
        )
        return prompt("persona") + "\n\n" + protocol

    async def ask(self, text: str, conversation_id: int | None = None) -> ChatTurn:
        text = text.strip()
        async with self.sessions() as s:
            conv = await s.get(ChatConversation, conversation_id) if conversation_id else None
            if conv is None:
                conv = ChatConversation(title=text[:120] or "New conversation")
                s.add(conv)
                await s.flush()
            history = list(
                await s.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv.id)
                    .order_by(ChatMessage.id.desc())
                    .limit(HISTORY_MESSAGES)
                )
            )[::-1]
            await s.commit()
            conv_id = conv.id
        # The question is saved with its answer (below), so searching past conversations
        # during this turn never finds the question itself.

        hit = (
            await self.routines.match(text)
            if self.routines is not None and self.may("routines.read")
            else None
        )
        if hit is not None:  # the fast path: a taught routine runs with no AI call
            turn = await self._run_routine(hit, conv_id)
        else:
            turn = await self._think(text, history, conv_id)
        async with self.sessions() as s:
            s.add(ChatMessage(conversation_id=conv_id, role="user", content=text))
            s.add(
                ChatMessage(
                    conversation_id=conv_id,
                    role="assistant",
                    content=turn.reply,
                    steps=turn.steps or None,
                    cards=turn.cards or None,
                    model=turn.model,
                    error=turn.error,
                    memory_ops=turn.memory or None,
                )  # fmt: skip
            )
            conv = await s.get_one(ChatConversation, conv_id)
            conv.updated_at = utcnow()
            await s.commit()
        return turn

    async def _run_routine(self, hit: Match, conversation_id: int) -> ChatTurn:
        """Run each step in order through the registry (permissions and audit apply), and
        stop at the first one that fails or needs your confirmation."""
        routine = hit.routine
        now = local_now(self.tz_name)
        turn = ChatTurn(conversation_id=conversation_id, reply="", model="routine")
        lines = []
        for st in routine.steps:
            args = render(st.get("args") or {}, hit.said, now)
            outcome = await self.skills.execute(st["tool"], args, actor="routine")
            turn.steps.append({"tool": st["tool"], "args": json.dumps(args, ensure_ascii=False),
                               "ok": outcome.ok, "summary": outcome.summary})  # fmt: skip
            if outcome.display:
                turn.cards.append(outcome.display)
            if not outcome.ok:
                lines.append(f"- ✕ {outcome.summary}")
                lines.append("\nStopped here. Ask me directly to do this step.")
                turn.error = True
                break
            lines.append(f"- {outcome.summary}")
        await self.routines.mark_used(routine.id)  # type: ignore[union-attr]
        turn.reply = f"**{routine.name}**\n\n" + "\n".join(lines)
        return turn

    async def _think(self, text: str, history: list[ChatMessage], conv_id: int) -> ChatTurn:
        previous_user = next((m.content for m in reversed(history) if m.role == "user"), "")
        remembered = (
            await recall(self.memory, text, previous_user)
            if self.memory and self.may("memory.read")
            else None
        )
        memory_block = remembered.block if remembered else ""
        if self.memory and not self.may("memory.write"):
            memory_block += (
                "\n<note>The user has turned off saving memories: memory changes are ignored, "
                "so don't say you'll remember anything.</note>"
            )
        # Follow-ups ("move it to friday") keep the tools the last answer used.
        last = next((m for m in reversed(history) if m.role == "assistant"), None)
        sticky = {st["tool"].split(".")[0] for st in (last.steps or [])} if last else set()
        groups = self.skills.groups_for(text, sticky)
        turn = await self._run(text, history, conv_id, memory_block, groups)
        turn.conversation_id = conv_id
        if remembered and remembered.relevant_ids:
            await self.memory.mark_used(remembered.relevant_ids)  # type: ignore[union-attr]
        return turn

    async def _run(
        self,
        text: str,
        history: list[ChatMessage],
        conversation_id: int,
        memory_block: str,
        groups: set[str] | None = None,
    ) -> ChatTurn:
        system = self.system_prompt(groups)
        results: list[str] = []
        done_calls: set[tuple[str, str]] = set()
        turn = ChatTurn(conversation_id=conversation_id, reply="")
        for i in range(MAX_TOOL_STEPS + 1):
            last_chance = i == MAX_TOOL_STEPS
            req = JSONRequest(
                system=system,
                user=self._transcript(
                    history, text, results, last_chance, memory_block, turn.memory
                ),
                schema=STEP_SCHEMA,
                schema_name="neurovex_step",
                max_tokens=STEP_MAX_TOKENS,
                task="chat",
                self_described=True,  # protocol.md shows the format; no schema tokens
            )
            try:
                step, result = await self.router.complete_json(
                    req, parse_step, max_wait_s=CHAT_MAX_WAIT_S
                )
            except ProvidersExhausted as e:
                log.warning("chat_providers_unavailable", error=str(e)[:300])
                if any(s["ok"] for s in turn.steps):
                    # The tools already found the answer: show it rather than just "busy".
                    turn.reply = (
                        _fallback_reply(turn.steps)
                        + "\n\n(I couldn't reach my AI provider to write this up, so these are"
                        " the raw results.)"
                    )
                    return turn
                turn.reply = _unavailable_message(e)
                turn.error = True
                return turn
            turn.model = result.label
            await self._apply_memory(step.memory_ops(), conversation_id, turn)
            if step.action == "respond":
                turn.reply = step.message.strip()
                return turn
            if last_chance:  # the model wants yet another tool: answer with what we have
                turn.reply = step.message.strip() or _fallback_reply(turn.steps)
                return turn
            all_ok, changes_only = True, True
            for tool, args in step.calls():
                call = (tool, _canonical_args(args))
                if call in done_calls:  # repeating a call wastes tokens: point back to it
                    results.append(
                        f'<tool_result tool="{tool}">Already done above; use that result.'
                        "</tool_result>"
                    )
                    all_ok = False
                    continue
                done_calls.add(call)
                skill = self.skills.get(tool)
                outcome = await self.skills.execute(tool, args)
                turn.steps.append(
                    {"tool": tool, "args": args, "ok": outcome.ok, "summary": outcome.summary}
                )
                if outcome.display:
                    turn.cards.append(outcome.display)
                results.append(_fence(tool, args, outcome))
                all_ok = all_ok and outcome.ok
                changes_only = changes_only and skill is not None and skill.risk is not Risk.READ
            if step.message.strip() and all_ok and changes_only:
                # Actions that came with their reply (protocol: changes, the last tools):
                # done in one model call instead of two.
                turn.reply = step.message.strip()
                return turn
        turn.reply = _fallback_reply(turn.steps)  # pragma: no cover - loop always returns
        return turn

    async def _apply_memory(
        self, ops: list[MemoryOp], conversation_id: int, turn: ChatTurn
    ) -> None:
        if self.memory is None or not self.may("memory.write"):
            return
        for op in ops:
            if op.op == "add":
                m, how = await self.memory.add(
                    op.text, op.kind, op.importance, conversation_id=conversation_id
                )
                change = "remembered" if how == "added" else "updated"
            elif op.op == "update" and op.id:
                kind = op.kind if op.kind in KINDS else None
                m = await self.memory.update(
                    op.id, text_=op.text or None, kind=kind, importance=op.importance
                )
                change = "updated"
            elif op.op == "forget" and op.id:
                m = await self.memory.forget(op.id)
                change = "forgot"
            else:
                continue
            if m is None:
                continue
            entry = {"op": change, "id": m.id, "kind": m.kind, "text": m.text}
            turn.memory = [e for e in turn.memory if e["id"] != m.id] + [entry]
            log.info("memory_changed", op=change, id=m.id, kind=m.kind)

    @staticmethod
    def _transcript(
        history: list[ChatMessage],
        text: str,
        results: list[str],
        last_chance: bool,
        memory_block: str = "",
        memory_changes: list[dict[str, Any]] | None = None,
    ) -> str:
        parts: list[str] = [memory_block] if memory_block else []
        if history:
            lines = []
            for m in history:
                who = "User" if m.role == "user" else "Neurovex"
                used = ""
                if m.steps:
                    used = f" [used: {', '.join(s['tool'] for s in m.steps)}]"
                content = m.content
                if m.role != "user" and len(content) > HISTORY_REPLY_CHARS:
                    content = content[:HISTORY_REPLY_CHARS] + "…"
                lines.append(f"{who}{used}: {content}")
            parts.append("<history>\n" + "\n".join(lines) + "\n</history>")
        parts.append(f"<current_request>\nUser: {text}\n</current_request>")
        if memory_changes:  # so later steps don't go searching for what was just saved
            lines = [f"[m{m['id']}] {m['op']} ({m['kind']}) {m['text']}" for m in memory_changes]
            parts.append(
                "<memory_changes_this_turn>\n" + "\n".join(lines) + "\n</memory_changes_this_turn>"
            )
        if results:
            parts.append("<tool_results>\n" + "\n".join(results) + "\n</tool_results>")
        if last_chance:
            parts.append("You have used all your tool steps. Respond to the user now.")
        else:
            parts.append("Decide the next step: use a tool or respond.")
        return "\n\n".join(parts)

    async def conversations(self, limit: int = 20) -> list[ChatConversation]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(ChatConversation).order_by(ChatConversation.updated_at.desc()).limit(limit)
            )
            return list(rows)

    async def messages(self, conversation_id: int) -> list[ChatMessage] | None:
        async with self.sessions() as s:
            if await s.get(ChatConversation, conversation_id) is None:
                return None
            rows = await s.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id)
            )
            return list(rows)

    async def delete(self, conversation_id: int) -> bool:
        async with self.sessions() as s:
            conv = await s.get(ChatConversation, conversation_id)
            if conv is None:
                return False
            await s.delete(conv)
            await s.commit()
            return True


def _canonical_args(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw or "{}"), sort_keys=True)
    except json.JSONDecodeError:
        return raw.strip()


def _fallback_reply(steps: list[dict[str, Any]]) -> str:
    good = [s["summary"] for s in steps if s["ok"]]
    if not good:
        return "I couldn't work that out. Could you rephrase?"
    failed = len(steps) - len(good)
    note = f" ({failed} other step{'s' if failed > 1 else ''} failed.)" if failed else ""
    return f"Here's what I found: {'; '.join(good)}.{note}"


def _unavailable_message(e: ProvidersExhausted) -> str:
    wait = f" Try again in about {int(e.retry_in_s) + 1} seconds." if e.retry_in_s else ""
    if "No AI provider configured" in str(e):
        return "I can't think right now: no AI provider key is set up. Run `nvx llm setup`."
    return "My AI providers are busy or over their rate limits right now." + wait
