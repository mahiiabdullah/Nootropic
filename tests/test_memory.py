from __future__ import annotations

import json

import httpx
from sqlalchemy import text

from neurovex.conversation.engine import ChatEngine, parse_step
from neurovex.core.db import create_engine, ensure_fts
from neurovex.core.skills import SkillRegistry
from neurovex.llm.ratelimit import RateGate
from neurovex.main import create_app
from neurovex.memory.recall import MAX_CHARS, recall
from neurovex.memory.skills import memory_skills
from neurovex.memory.store import MemoryStore, fts_query, looks_secret, similarity
from tests.conftest import FakeSummarizer
from tests.test_llm import router_for

HEADERS = {"X-Neurovex-Client": "test"}


def step(message="", tool="", args=None, memory=None, action=None) -> str:
    return json.dumps(
        {
            "action": action or ("use_tool" if tool else "respond"),
            "tool": tool,
            "arguments_json": json.dumps(args or {}) if tool else "",
            "message": message,
            "memory": memory or [],
        }
    )


def op(kind="fact", text_="", op_="add", id_=0, importance=2):
    return {"op": op_, "id": id_, "kind": kind, "text": text_, "importance": importance}


# --- helpers ------------------------------------------------------------------------------


def test_fts_query_is_safe_and_skips_stopwords():
    # FTS syntax in user text ("OR", NEAR(), quotes) can't break the query.
    assert fts_query('What do you know about "Rust" OR NEAR(x)?') == '"rust"* OR "near"*'
    assert fts_query("what is it") is None
    q = fts_query("I'm learning C++ and Rust!")
    assert '"learning"*' in q and '"rust"*' in q and '"c++"*' in q


def test_secret_detection():
    assert looks_secret("My API key is gsk_abcdefghijklmnop1234")
    assert looks_secret("password: hunter2")
    assert looks_secret("card 4111 1111 1111 1111")
    assert looks_secret("pk_live_Gwhgt6ZOzfIi7szmPL")
    assert not looks_secret("Is learning Rust and prefers short answers")
    assert not looks_secret("Was born in 1998")


def test_similarity():
    assert similarity("Is learning Rust", "Is learning Rust now") > 0.6
    assert similarity("Is learning Rust", "Has a sister in Toronto") == 0


# --- store ----------------------------------------------------------------------------------


async def test_store_add_merge_reject_update_forget(sessions):
    store = MemoryStore(sessions)
    m, how = await store.add("Is learning Rust", "goal", 2)
    assert how == "added" and m.kind == "goal"
    again, how = await store.add("is learning rust", "fact", 3)
    assert how == "merged" and again.id == m.id and again.importance == 3
    assert (await store.add("My password is hunter2", "fact"))[0] is None
    assert (await store.add("  ", "fact"))[1] == "empty"
    odd, _ = await store.add("Likes long walks", "nonsense-kind")
    assert odd.kind == "fact"
    assert (
        await store.update(m.id, text_="Is learning Rust and Go")
    ).text == "Is learning Rust and Go"
    assert await store.update(m.id, text_="api key is sk-abcdefghijklmnop") is None
    assert await store.update(9999, text_="x y z") is None
    assert (await store.forget(m.id)).id == m.id and await store.forget(m.id) is None
    assert await store.count() == 1
    assert await store.clear() == 1 and await store.count() == 0


async def test_store_search_ranks_and_stems(sessions):
    store = MemoryStore(sessions)
    await store.add("Is learning Rust programming", "goal")
    await store.add("Sister Ayesha lives in Toronto", "person", 3)
    await store.add("Prefers short answers", "preference")
    found = await store.search("any rust learning resources?")
    assert [m.text for m in found] == ["Is learning Rust programming"]
    assert [m.text for m in await store.search("Where does my sister live?")] == [
        "Sister Ayesha lives in Toronto"
    ]
    assert await store.search("what is it") == []  # only stopwords: no query, no work


async def test_core_memories_first_instructions(sessions):
    store = MemoryStore(sessions)
    await store.add("Minor fact about tea", "fact", 1)
    inst, _ = await store.add("Call the user Mahi", "instruction", 2)
    core, _ = await store.add("Works as a backend developer", "fact", 3)
    pinned, _ = await store.add("Lives in Dhaka", "fact", 1, pinned=True)
    pref, _ = await store.add("Prefers dark mode", "preference", 1)
    assert [m.id for m in await store.core()] == [inst.id, pref.id, pinned.id, core.id]


# --- recall -------------------------------------------------------------------------------------


async def test_recall_block_is_small_and_relevant(sessions):
    store = MemoryStore(sessions)
    await store.add("Call the user Mahi", "instruction", 3)
    rust, _ = await store.add("Is learning Rust", "goal", 2)
    await store.add("Owns a cat named Miso", "fact", 1)
    for i in range(40):
        await store.add(f"Unrelated detail number {i} about gardening plot {i * 7}", "fact", 1)

    r = await recall(store, "Recommend a Rust project")
    assert r.block.startswith("<memory>") and "Call the user Mahi" in r.block
    assert f"[m{rust.id}] (goal) Is learning Rust" in r.block
    assert "Miso" not in r.block and len(r.block) <= MAX_CHARS + 40
    assert r.relevant_ids == [rust.id]
    assert (await recall(MemoryStore(sessions), "")).block.count("[m") >= 1
    empty = await recall(store, "zzzz qqqq")
    assert "Is learning Rust" not in empty.block  # only core memories


# --- chat engine with memory ---------------------------------------------------------------------


def chat(settings, sessions, script) -> ChatEngine:
    router = router_for(settings, {"groq": script})
    router.states["groq"].gate = RateGate()
    store = MemoryStore(sessions)
    skills = SkillRegistry()
    skills.register_all(memory_skills(store))
    return ChatEngine(router, skills, sessions, store)


async def test_chat_remembers_without_extra_calls(settings, sessions):
    engine = chat(settings, sessions, [
        step("Noted, Rust it is.", memory=[op("goal", "Is learning Rust", importance=3),
                                           op("instruction", "Keep answers short", importance=3)]),
        step("You're learning Rust, so try Rustlings."),
    ])  # fmt: skip
    first = await engine.ask("I'm learning Rust. Keep answers short please.")
    assert [(m["op"], m["text"]) for m in first.memory] == [
        ("remembered", "Is learning Rust"), ("remembered", "Keep answers short"),
    ]  # fmt: skip
    provider = engine.router.states["groq"].provider
    assert provider.calls == 1  # remembering added no model call

    second = await engine.ask("Suggest something to practice", None)  # brand-new conversation
    prompt = provider.requests[1].user
    assert (
        prompt.startswith("<memory>")
        and "Is learning Rust" in prompt
        and "Keep answers short" in prompt
    )
    assert second.reply.startswith("You're learning Rust")
    msgs = await engine.messages(first.conversation_id)
    assert msgs[-1].memory_ops[0]["text"] == "Is learning Rust"


async def test_chat_updates_and_forgets_by_id(settings, sessions):
    store = MemoryStore(sessions)
    city, _ = await store.add("Lives in Dhaka", "fact", 2)
    pet, _ = await store.add("Has a dog named Bolt", "fact", 2)
    engine = chat(settings, sessions, [
        step("Updated.", memory=[op("fact", "Lives in Chattogram", "update", city.id),
                                 op("fact", "", "forget", pet.id),
                                 op("fact", "", "forget", 4242),  # unknown id: ignored
                                 {"op": "explode"}]),  # malformed: skipped, step still valid
    ])  # fmt: skip
    turn = await engine.ask("I moved to Chattogram, and forget about the dog.")
    assert [(m["op"], m["id"]) for m in turn.memory] == [("updated", city.id), ("forgot", pet.id)]
    assert (await store.get(city.id)).text == "Lives in Chattogram"
    assert await store.get(pet.id) is None


async def test_secrets_from_the_model_are_not_stored(settings, sessions):
    engine = chat(
        settings, sessions, [step("Ok.", memory=[op("fact", "API key is gsk_abcdefghijklmnop99")])]
    )
    turn = await engine.ask("my key is gsk_abcdefghijklmnop99")
    assert turn.memory == [] and await MemoryStore(sessions).count() == 0


async def test_memory_search_finds_old_conversations(settings, sessions):
    engine = chat(settings, sessions, [
        step("Tokio is a good async runtime for Rust."),
        step("", tool="memory.search", args={"query": "async runtime"}),
        step("Last time we talked about Tokio."),
    ])  # fmt: skip
    await engine.ask("Which async runtime should I use?")
    turn = await engine.ask("What did we discuss about runtimes?")  # new conversation
    assert turn.steps[0]["tool"] == "memory.search" and "2 chat matches" in turn.steps[0]["summary"]
    result_prompt = engine.router.states["groq"].provider.requests[-1].user
    assert "«async»" in result_prompt and "«runtime»" in result_prompt


def test_steps_without_memory_field_still_parse():
    s = parse_step('{"action": "respond", "tool": "", "arguments_json": "", "message": "hi"}')
    assert s.memory == [] and s.memory_ops() == []


# --- upgrade path: index built from rows that existed before it ------------------------------------


async def test_ensure_fts_indexes_existing_rows(settings):
    settings.ensure_dirs()
    engine = create_engine(settings.db_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"))
        await conn.execute(text("INSERT INTO notes (body) VALUES ('old note about kubernetes')"))
        await conn.run_sync(lambda c: ensure_fts(c, table="notes", fts="notes_fts", column="body"))
        await conn.run_sync(lambda c: ensure_fts(c, table="notes", fts="notes_fts", column="body"))
        await conn.execute(text("INSERT INTO notes (body) VALUES ('new note about docker')"))
        hits = (
            await conn.execute(
                text("SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'kubernetes OR docker'")
            )
        ).all()
    await engine.dispose()
    assert sorted(r[0] for r in hits) == [1, 2]


# --- API ------------------------------------------------------------------------------------------


async def test_memory_api(settings):
    async def factory(_s, _h):
        return FakeSummarizer()

    app = create_app(settings, factory, llm=router_for(settings, {"groq": []}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            assert (await c.post("/api/memory", json={"text": "Likes tea"})).status_code == 403
            r = await c.post(
                "/api/memory",
                json={"text": "Likes green tea", "kind": "preference"},
                headers=HEADERS,
            )
            assert r.status_code == 201 and r.json()["source"] == "user"
            mid = r.json()["id"]
            bad = await c.post("/api/memory", json={"text": "password is hunter2"}, headers=HEADERS)
            assert bad.status_code == 422
            assert (
                await c.post("/api/memory", json={"text": "x y z", "kind": "nope"}, headers=HEADERS)
            ).status_code == 422
            patched = await c.patch(
                f"/api/memory/{mid}", json={"pinned": True, "importance": 3}, headers=HEADERS
            )
            assert patched.json()["pinned"] is True and patched.json()["importance"] == 3
            assert (
                await c.patch("/api/memory/999", json={"pinned": True}, headers=HEADERS)
            ).status_code == 404
            assert [m["text"] for m in (await c.get("/api/memory")).json()] == ["Likes green tea"]
            found = (await c.get("/api/memory/search", params={"q": "tea"})).json()
            assert found["memories"][0]["id"] == mid and found["conversations"] == []
            assert (
                await c.delete("/api/memory", headers=HEADERS, params={"confirm": "no"})
            ).status_code == 400
            assert (await c.delete(f"/api/memory/{mid}", headers=HEADERS)).status_code == 204
            await c.post("/api/memory", json={"text": "Likes coffee"}, headers=HEADERS)
            cleared = await c.delete(
                "/api/memory", headers=HEADERS, params={"confirm": "forget-everything"}
            )
            assert cleared.json() == {"forgotten": 1}


async def test_preferences_always_come_back(sessions):
    store = MemoryStore(sessions)
    pref, _ = await store.add("Prefers short answers", "preference", 2)
    r = await recall(store, "What do you know about me?")  # all stopwords: no keyword match
    assert f"[m{pref.id}] (preference) Prefers short answers" in r.block


async def test_memory_changes_are_visible_to_later_steps(settings, sessions):
    engine = chat(settings, sessions, [
        step("", tool="memory.list", memory=[op("fact", "Name is Mahi", importance=3)]),
        step("You're Mahi."),
    ])  # fmt: skip
    await engine.ask("I'm Mahi. What do you know about me?")
    second = engine.router.states["groq"].provider.requests[1].user
    assert "<memory_changes_this_turn>" in second and "remembered (fact) Name is Mahi" in second
