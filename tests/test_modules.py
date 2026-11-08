"""Modules start safely in order; permissions decide what chat may see and change."""

from __future__ import annotations

import httpx
import pytest
import respx

from neurovex.conversation.engine import ChatEngine
from neurovex.core.modules import ModuleManager, ModuleManifest
from neurovex.core.skills import Risk, Skill, SkillRegistry, SkillResult
from neurovex.llm.ratelimit import RateGate
from neurovex.main import create_app
from neurovex.memory.skills import memory_skills
from neurovex.memory.store import MemoryStore
from neurovex.security.audit import AuditLog
from neurovex.security.permissions import PermissionStore
from tests.conftest import rss, signed_in
from tests.test_llm import router_for
from tests.test_memory import op, step

WEB = {"X-Neurovex-Client": "web"}


class Fake:
    def __init__(self, mid: str, depends_on=(), fail: bool = False, log=None) -> None:
        self.manifest = ModuleManifest(id=mid, name=mid.title(), depends_on=list(depends_on))
        self.fail, self.log = fail, log if log is not None else []

    async def start(self) -> None:
        self.log.append(f"start {self.manifest.id}")
        if self.fail:
            raise RuntimeError("no network")

    async def stop(self) -> None:
        self.log.append(f"stop {self.manifest.id}")


async def test_modules_start_after_what_they_need_and_stop_in_reverse():
    log: list[str] = []
    m = ModuleManager()
    m.add(Fake("today", ["tasks", "news"], log=log))
    m.add(Fake("news", log=log))
    m.add(Fake("tasks", log=log))
    await m.start_all()
    assert log == ["start tasks", "start news", "start today"]  # as today lists them
    await m.stop_all()
    assert log[3:] == ["stop today", "stop news", "stop tasks"]


async def test_a_broken_module_is_degraded_and_the_rest_keep_running():
    log: list[str] = []
    m = ModuleManager()
    m.add(Fake("news", fail=True, log=log))
    m.add(Fake("tasks", log=log))
    m.add(Fake("today", ["news"], log=log))
    await m.start_all()
    states = {s["id"]: (s["state"], s["error"]) for s in m.status()}
    assert states == {
        "news": ("degraded", "RuntimeError: no network"),
        "tasks": ("running", None),
        "today": ("degraded", "needs news"),
    }
    assert "start today" not in log  # never started on a broken dependency
    await m.stop_all()
    assert "stop news" in log  # degraded modules may still hold clients to close


def test_unknown_dependencies_and_loops_are_caught():
    m = ModuleManager()
    m.add(Fake("today", ["calendar"]))
    with pytest.raises(ValueError, match="unknown module calendar"):
        m.start_order()
    m = ModuleManager()
    m.add(Fake("a", ["b"]))
    m.add(Fake("b", ["a"]))
    with pytest.raises(ValueError, match="loop"):
        m.start_order()
    with pytest.raises(ValueError, match="already registered"):
        m.add(Fake("a"))


async def test_permissions_start_allowed_and_your_choices_are_kept(sessions):
    store = PermissionStore(sessions)
    await store.load(["tasks.read", "tasks.write"])
    assert store.allowed("tasks.write") and not store.allowed("calendar.write")  # undeclared
    await store.set("tasks.write", False)
    again = PermissionStore(sessions)
    await again.load(["tasks.read", "tasks.write", "news.read"])  # a new module arrived
    assert not again.allowed("tasks.write") and again.allowed("news.read")
    assert (await again.decided_by())["tasks.write"] == "user"
    with pytest.raises(KeyError):
        await again.set("calendar.write", True)


async def test_skills_need_their_module_permission(sessions):
    store = PermissionStore(sessions)
    await store.load(["tasks.read", "tasks.write"])
    audit = AuditLog(sessions)
    reg = SkillRegistry(audit=audit.append, allowed=store.allowed)

    async def ok(_p) -> SkillResult:
        return SkillResult(ok=True, summary="done")

    reg.register(Skill("tasks.list", "List", ok))
    reg.register(Skill("tasks.add", "Add", ok, risk=Risk.LOCAL_WRITE))
    reg.register(Skill("system.time", "Time", ok, permissions=[]))
    assert reg.get("tasks.add").needs == ["tasks.write"] and reg.get("system.time").needs == []
    await store.set("tasks.write", False)
    assert (await reg.execute("tasks.list", {})).ok
    denied = await reg.execute("tasks.add", {})
    assert not denied.ok and "changing tasks" in denied.error and "Settings" in denied.error
    assert (await reg.execute("system.time", {})).ok
    assert [e.outcome for e in await audit.tail(5)] == ["ok", "denied", "ok"]


def _engine(settings, sessions, store: PermissionStore, script) -> ChatEngine:
    router = router_for(settings, {"groq": script})
    router.states["groq"].gate = RateGate()
    memory = MemoryStore(sessions)
    skills = SkillRegistry(allowed=store.allowed)
    skills.register_all(memory_skills(memory))
    return ChatEngine(router, skills, sessions, memory, allowed=store.allowed)


async def test_memory_can_be_kept_out_of_chat(settings, sessions):
    store = PermissionStore(sessions)
    await store.load(["memory.read", "memory.write"])
    await MemoryStore(sessions).add("Lives in Dhaka", "fact", 3, pinned=True)
    await store.set("memory.read", False)
    engine = _engine(settings, sessions, store, [step("Hi!")])
    await engine.ask("Where do I live?")
    sent = engine.router.states["groq"].provider.requests[0]
    assert "Dhaka" not in sent.user and "Dhaka" not in sent.system  # nothing recalled


async def test_saving_memories_can_be_turned_off(settings, sessions):
    store = PermissionStore(sessions)
    await store.load(["memory.read", "memory.write"])
    await store.set("memory.write", False)
    engine = _engine(settings, sessions, store, [step("Okay.", memory=[op("fact", "Has a cat")])])
    turn = await engine.ask("I have a cat")
    assert turn.memory == [] and await MemoryStore(sessions).list() == []
    sent = engine.router.states["groq"].provider.requests[0]
    assert "turned off saving memories" in sent.user + sent.system


@pytest.fixture
async def app(settings, fake_summarizer):
    async def factory(_settings, _http):
        return fake_summarizer

    app = create_app(settings, factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https?://.*").respond(200, content=rss())
        async with app.router.lifespan_context(app):
            yield app


def client(app, settings) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers=signed_in(settings)
    )


async def test_the_app_lists_its_modules_skills_and_permissions(app, settings):
    async with client(app, settings) as c:
        mods = {m["id"]: m["state"] for m in (await c.get("/api/system/modules")).json()}
        assert mods == {
            "news": "running", "tasks": "running", "memory": "running", "routines": "running",
            "today": "running", "backups": "running",
        }  # fmt: skip
        assert (await c.get("/health")).json()["degraded"] == []
        skills = {s["id"]: s for s in (await c.get("/api/skills")).json()}
        assert skills["tasks.add"]["needs"] == ["tasks.write"] and skills["tasks.add"]["allowed"]
        assert set(skills["today.brief"]["needs"]) == {"tasks.read", "news.read", "memory.read"}
        perms = {p["permission"]: p for p in (await c.get("/api/permissions")).json()}
        assert set(perms) == {
            "news.read", "news.write", "tasks.read", "tasks.write", "memory.read",
            "memory.write", "routines.read", "routines.write",
        }  # fmt: skip
        assert perms["tasks.write"]["label"] == "changing tasks" and perms["tasks.write"]["granted"]

        r = await c.post("/api/permissions/tasks.write", json={"granted": False}, headers=WEB)
        assert r.status_code == 200
        skills = {s["id"]: s for s in (await c.get("/api/skills")).json()}
        assert not skills["tasks.add"]["allowed"] and skills["tasks.list"]["allowed"]
        assert (
            await c.post("/api/permissions/x.y", json={"granted": True}, headers=WEB)
        ).status_code == 404
        # The pages still work: permissions only limit chat.
        assert (await c.post("/api/tasks", json={"text": "Call mum"}, headers=WEB)).is_success
    actions = [e.action for e in await app.state.audit.tail(5)]
    assert "permission.deny" in actions
