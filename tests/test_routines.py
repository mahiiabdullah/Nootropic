"""Routines: taught in chat, run by saying a phrase, with no AI call."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
import respx
from pydantic import BaseModel

from neurovex.conversation.engine import ChatEngine
from neurovex.core.skills import Risk, Skill, SkillRegistry, SkillResult
from neurovex.llm.ratelimit import RateGate
from neurovex.main import create_app
from neurovex.routines.models import Routine
from neurovex.routines.skills import routine_skills
from neurovex.routines.store import RoutineError, RoutineStore, match_routine, normalize, render
from neurovex.security.permissions import PermissionStore
from tests.conftest import rss, signed_in
from tests.test_llm import router_for
from tests.test_memory import step

WEB = {"X-Neurovex-Client": "web"}


def r(name: str, *triggers: str) -> Routine:
    return Routine(id=1, name=name, triggers=[normalize(t) for t in triggers], steps=[])


@pytest.mark.parametrize(
    ("said", "runs", "rest"),
    [
        ("start my day", True, ""),
        ("  Start my DAY! ", True, ""),
        ("start my dya", True, ""),  # a typo
        ("log water 2 glasses", True, "2 glasses"),
        ("Log water: half a litre", True, "half a litre"),
        ("how do I start my day better?", False, None),
        ("start", False, None),
        ("water", False, None),
        ("", False, None),
    ],
)
def test_matching_is_strict_so_questions_are_left_alone(said, runs, rest):
    routines = [r("Start my day", "start my day"), r("Water", "log water")]
    hit = match_routine(routines, said)
    assert (hit is not None) == runs
    if runs:
        assert hit.said == rest


def test_templates_are_filled_at_any_depth():
    now = datetime(2026, 11, 9, 8, 5)
    args = {"title": "Buy {{input}}", "due": "{{ date }}", "tags": ["{{time}}"], "n": 3}
    assert render(args, "milk", now) == {
        "title": "Buy milk", "due": "2026-11-09", "tags": ["08:05"], "n": 3,
    }  # fmt: skip


class AddParams(BaseModel):
    title: str
    priority: str = "normal"


def registry_with_fakes(log: list[str]) -> SkillRegistry:
    reg = SkillRegistry()

    async def brief(_p: BaseModel) -> SkillResult:
        log.append("today.brief")
        return SkillResult(ok=True, summary="Today: 3 due", display={"type": "today"})

    async def add(p: AddParams) -> SkillResult:
        log.append(f"tasks.add:{p.title}")
        return SkillResult(ok=True, summary=f"Added '{p.title}'")

    async def broken(_p: BaseModel) -> SkillResult:
        return SkillResult.fail("The news service is down")

    async def wipe(_p: BaseModel) -> SkillResult:
        log.append("wiped")
        return SkillResult(ok=True, summary="wiped")

    reg.register(Skill("today.brief", "Day", brief))
    reg.register(Skill("tasks.add", "Add", add, AddParams, risk=Risk.LOCAL_WRITE))
    reg.register(Skill("news.top", "Top", broken))
    reg.register(Skill("memory.clear", "Clear", wipe, risk=Risk.DESTRUCTIVE))
    return reg


async def _teach(reg, store, **params) -> SkillResult:
    reg.register_all(routine_skills(store, reg))
    return await reg.execute("routines.save", params)


async def test_teaching_checks_every_step(sessions):
    reg, store = registry_with_fakes([]), RoutineStore(sessions)
    bad_tool = await _teach(reg, store, name="X", triggers=["do x"], steps=[{"tool": "nope.go"}])
    assert not bad_tool.ok and "isn't a tool" in bad_tool.error
    loop = await reg.execute("routines.save", {"name": "X", "triggers": ["do x"],
                                               "steps": [{"tool": "routines.list"}]})  # fmt: skip
    assert not loop.ok  # a routine can't run routines
    bad_args = await reg.execute("routines.save", {"name": "X", "triggers": ["do x"],
                                                   "steps": [{"tool": "tasks.add"}]})  # fmt: skip
    assert not bad_args.ok and "title" in bad_args.error
    ok = await reg.execute("routines.save", {
        "name": "Groceries", "triggers": ["Add groceries", "groceries"],
        "steps": [{"tool": "tasks.add", "args": {"title": "Buy {{input}}"}}],
    })  # fmt: skip
    assert ok.ok and "say 'add groceries' or 'groceries'" in ok.summary
    clash = await reg.execute("routines.save", {"name": "Other", "triggers": ["groceries"],
                                                "steps": [{"tool": "today.brief"}]})  # fmt: skip
    assert not clash.ok and "already runs the routine 'Groceries'" in clash.error
    again = await reg.execute("routines.save", {"name": "groceries", "triggers": ["shop"],
                                                "steps": [{"tool": "today.brief"}]})  # fmt: skip
    assert again.ok and again.summary.startswith("Updated")
    assert [(x.name, x.triggers) for x in await store.list()] == [("groceries", ["shop"])]


async def test_store_rejects_empty_routines(sessions):
    store = RoutineStore(sessions)
    with pytest.raises(RoutineError):
        await store.save("Nothing", ["do nothing"], [])
    with pytest.raises(RoutineError):
        await store.save("Quiet", ["  !! "], [{"tool": "today.brief", "args": {}}])


def _engine(settings, sessions, reg, script=(), allowed=None) -> ChatEngine:
    router = router_for(settings, {"groq": list(script)})
    router.states["groq"].gate = RateGate()
    return ChatEngine(router, reg, sessions, allowed=allowed, routines=RoutineStore(sessions))


async def test_saying_the_phrase_runs_the_steps_with_no_ai(settings, sessions):
    log: list[str] = []
    reg = registry_with_fakes(log)
    engine = _engine(settings, sessions, reg)
    reg.register_all(routine_skills(engine.routines, reg))
    await engine.routines.save("Start my day", ["start my day"], [
        {"tool": "today.brief", "args": {}},
        {"tool": "tasks.add", "args": {"title": "Plan {{date}}"}},
    ])  # fmt: skip
    turn = await engine.ask("Start my day!")
    assert engine.router.states["groq"].provider.requests == []  # no AI call at all
    assert log[0] == "today.brief" and log[1].startswith("tasks.add:Plan 20")
    assert turn.reply.startswith("**Start my day**") and "Today: 3 due" in turn.reply
    assert turn.model == "routine" and turn.cards == [{"type": "today"}] and not turn.error
    assert [s["tool"] for s in turn.steps] == ["today.brief", "tasks.add"]
    used = (await engine.routines.list())[0]
    assert used.use_count == 1 and used.last_used_at is not None
    again = await engine.ask("what's next?", turn.conversation_id)  # not a routine: AI answers
    assert again.error  # the scripted fake has nothing left to say, so this one asked the AI


async def test_words_after_the_phrase_fill_input(settings, sessions):
    log: list[str] = []
    reg = registry_with_fakes(log)
    engine = _engine(settings, sessions, reg)
    await engine.routines.save("Shop", ["add groceries"],
                               [{"tool": "tasks.add", "args": {"title": "Buy {{input}}"}}])  # fmt: skip
    await engine.ask("add groceries oat milk")
    assert log == ["tasks.add:Buy oat milk"]


async def test_a_failing_or_unconfirmed_step_stops_the_routine(settings, sessions):
    log: list[str] = []
    reg = registry_with_fakes(log)
    engine = _engine(settings, sessions, reg)
    await engine.routines.save("Morning", ["morning"], [
        {"tool": "news.top", "args": {}}, {"tool": "today.brief", "args": {}},
    ])  # fmt: skip
    turn = await engine.ask("morning")
    assert turn.error and "Stopped here" in turn.reply and log == []
    await engine.routines.save("Wipe", ["wipe it"], [{"tool": "memory.clear", "args": {}}])
    turn = await engine.ask("wipe it")
    assert "needs the user's confirmation" in turn.reply and "wiped" not in log


async def test_routines_respect_what_chat_may_do(settings, sessions):
    log: list[str] = []
    perms = PermissionStore(sessions)
    await perms.load(["tasks.read", "tasks.write", "routines.read", "routines.write"])
    reg = registry_with_fakes(log)
    reg._allowed = perms.allowed
    engine = _engine(settings, sessions, reg, script=[step("Sure.")], allowed=perms.allowed)
    await engine.routines.save("Shop", ["add groceries"],
                               [{"tool": "tasks.add", "args": {"title": "Buy {{input}}"}}])  # fmt: skip
    await perms.set("tasks.write", False)
    turn = await engine.ask("add groceries eggs")
    assert "changing tasks" in turn.reply and log == []
    await perms.set("routines.read", False)  # routines off: the phrase is just a message
    turn = await engine.ask("add groceries eggs")
    assert turn.reply == "Sure." and log == []


async def test_teaching_in_chat_then_running_and_deleting_on_the_page(settings, fake_summarizer):
    async def factory(_settings, _http):
        return fake_summarizer

    teach = step("", tool="routines.save", args={
        "name": "Start my day", "triggers": ["start my day"],
        "steps": [{"tool": "today.brief", "args": {}}, {"tool": "tasks.next", "args": {}}],
    })  # fmt: skip
    router = router_for(settings, {"groq": [teach, step("Saved: say 'start my day'.")]})
    router.states["groq"].gate = RateGate()
    app = create_app(settings, factory, llm=router)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https?://.*").respond(200, content=rss())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1",
                                         headers=signed_in(settings)) as c:  # fmt: skip
                said = "When I say start my day, show my day and what to do next"
                taught = (await c.post("/api/chat", json={"message": said}, headers=WEB)).json()
                assert taught["steps"][0]["tool"] == "routines.save" and taught["steps"][0]["ok"]
                ran = (await c.post("/api/chat", json={"message": "start my day"},
                                    headers=WEB)).json()  # fmt: skip
                assert [s["tool"] for s in ran["steps"]] == ["today.brief", "tasks.next"]
                assert all(s["ok"] for s in ran["steps"]) and ran["reply"].startswith("**Start")
                listed = (await c.get("/api/routines")).json()
                assert listed[0]["triggers"] == ["start my day"] and listed[0]["use_count"] == 1
                assert listed[0]["steps"] == "today.brief → tasks.next"
                gone = await c.delete(f"/api/routines/{listed[0]['id']}", headers=WEB)
                assert gone.status_code == 200 and (await c.get("/api/routines")).json() == []
    entries = await app.state.audit.tail(10)
    assert [e.actor for e in entries if e.action == "today.brief"] == ["routine"]
    assert json.dumps([e.action for e in entries]).count("routines.delete") == 1
