from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from neurovex.conversation.engine import MAX_TOOL_STEPS, ChatEngine
from neurovex.core.skills import Risk, Skill, SkillRegistry, SkillResult
from neurovex.llm.errors import RateLimited
from neurovex.llm.ratelimit import RateGate
from neurovex.main import build_chat, create_app
from neurovex.modules.news.module import NewsModule
from tests.conftest import FakeSummarizer, hours_ago, rss
from tests.test_llm import router_for


def use(tool: str, **args) -> str:
    return json.dumps(
        {"action": "use_tool", "tool": tool, "arguments_json": json.dumps(args), "message": ""}
    )


def say(message: str) -> str:
    return json.dumps({"action": "respond", "tool": "", "arguments_json": "", "message": message})


# --- skill registry ----------------------------------------------------------------------


class EchoParams(BaseModel):
    text: str


async def test_registry_validates_and_never_raises():
    async def echo(p: EchoParams) -> SkillResult:
        return SkillResult(ok=True, summary=f"echo {p.text}", data=p.text)

    async def boom(_: BaseModel) -> SkillResult:
        raise RuntimeError("kaput")

    reg = SkillRegistry()
    reg.register_all(
        [
            Skill("t.echo", "Echo text.", echo, EchoParams),
            Skill("t.boom", "Explodes.", boom),
            Skill("t.delete", "Deletes things.", echo, EchoParams, Risk.DESTRUCTIVE),
        ]
    )
    with pytest.raises(ValueError):
        reg.register(Skill("t.echo", "dup", echo))
    assert (await reg.execute("t.echo", '{"text": "hi"}')).data == "hi"
    assert (await reg.execute("t.echo", {"text": "hi"})).summary == "echo hi"
    bad = (await reg.execute("t.echo", "{}")).error
    assert bad == "Invalid arguments for t.echo: text: Field required (got {})"  # short, readable
    assert "Invalid arguments" in (await reg.execute("t.echo", "not json")).error
    assert "Unknown tool" in (await reg.execute("t.nope", "{}")).error
    assert "kaput" in (await reg.execute("t.boom", "")).error
    assert "confirmation" in (await reg.execute("t.delete", {"text": "x"})).error
    assert (await reg.execute("t.delete", {"text": "x"}, confirmed=True)).ok
    assert "- t.echo(text: str): Echo text." in reg.describe()


# --- fixtures: a news module with stories + a scripted router ------------------------------


FEED = "https://feed.test/rss"


@pytest.fixture
async def news(settings, sessions):
    async def factory(_s, _h):
        return FakeSummarizer()

    module = NewsModule(settings, sessions, factory)
    async with sessions() as s:
        from neurovex.modules.news.models import Source

        s.add(Source(name="Feed", url=FEED))
        await s.commit()
    with respx.mock:
        respx.get(FEED).respond(
            200,
            content=rss(
                ("New AI model beats benchmarks", "https://n.test/ai", hours_ago(1)),
                ("Rust 2.0 released", "https://n.test/rust", hours_ago(2)),
                ("Critical Windows flaw patched", "https://n.test/win", hours_ago(3)),
            ),
        )
        await module.pipeline.run()
    yield module
    await module.stop()


def chat_for(settings, news, sessions, script) -> ChatEngine:
    router = router_for(settings, {"groq": script})
    router.states["groq"].gate = RateGate()  # rate pacing is covered in test_ratelimit
    return build_chat(router, news, sessions)


# --- news skills ----------------------------------------------------------------------------


async def test_news_skills_end_to_end(settings, sessions, news):
    reg = build_chat(router_for(settings, {"groq": []}), news, sessions).skills
    found = await reg.execute("news.search", {"query": "rust"})
    assert found.ok and [a["title"] for a in found.data] == ["Rust 2.0 released"]
    assert found.display["kind"] == "articles" and found.display["items"][0]["url"]
    top = await reg.execute("news.top", {})
    assert {a["title"] for a in top.data} >= {"Critical Windows flaw patched"}
    one = await reg.execute("news.article", {"id": found.data[0]["id"]})
    assert one.data["why_it_matters"] == "It matters." and one.data["url"]
    assert not (await reg.execute("news.article", {"id": 9999})).ok

    assert (await reg.execute("news.follow", {"value": "Rust"})).ok
    assert (
        await reg.execute("news.mute", {"value": "ai", "kind": "category"})
    ).summary == "Muted category 'AI'"
    assert not (await reg.execute("news.follow", {"value": "Space", "kind": "category"})).ok
    interests = (await reg.execute("news.interests", {})).data
    assert interests["following"] == ["Rust (topic)"] and interests["muted"] == ["AI (category)"]
    assert (await reg.execute("news.unfollow", {"value": "rust"})).data == {"done": True}
    assert (await reg.execute("news.briefing", {})).data["headline"] == "Your news, briefly"
    assert (await reg.execute("system.time", {})).ok


# --- chat engine ------------------------------------------------------------------------------


async def test_chat_uses_a_tool_then_answers_and_remembers(settings, sessions, news):
    engine = chat_for(settings, news, sessions, [
        use("news.search", query="rust"),
        say("Rust 2.0 is out (Feed)."),
        say("You asked about Rust a moment ago."),
    ])  # fmt: skip
    turn = await engine.ask("Anything new about Rust?")
    assert turn.reply == "Rust 2.0 is out (Feed)." and not turn.error
    assert turn.steps == [
        {
            "tool": "news.search",
            "args": '{"query": "rust"}',
            "ok": True,
            "summary": "Found 1 stories for 'rust'; showing 1",
        }  # fmt: skip
    ]
    assert turn.cards[0]["items"][0]["title"] == "Rust 2.0 released"
    assert turn.model == "groq/fake-groq"

    provider = engine.router.states["groq"].provider
    second_prompt = provider.requests[1].user
    assert (
        '<tool_result tool="news.search"' in second_prompt and "Rust 2.0 released" in second_prompt
    )
    assert "news.search" in provider.requests[0].system  # tool catalog in the system prompt

    follow_up = await engine.ask("And?", turn.conversation_id)
    assert follow_up.conversation_id == turn.conversation_id
    history = provider.requests[2].user
    assert (
        "User: Anything new about Rust?" in history and "Neurovex [used: news.search]:" in history
    )

    msgs = await engine.messages(turn.conversation_id)
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[1].cards and msgs[1].steps
    assert [c.title for c in await engine.conversations()] == ["Anything new about Rust?"]


async def test_chat_recovers_from_a_bad_tool_call(settings, sessions, news):
    engine = chat_for(
        settings, news, sessions, [use("news.nope"), say("Sorry, let me answer directly.")]
    )
    turn = await engine.ask("hi")
    assert turn.steps[0]["ok"] is False and "Unknown tool" in turn.steps[0]["summary"]
    assert turn.reply == "Sorry, let me answer directly."


async def test_chat_stops_after_max_tool_steps(settings, sessions, news):
    script = [use("news.search", query=f"q{i}") for i in range(MAX_TOOL_STEPS + 1)]
    engine = chat_for(settings, news, sessions, script)
    turn = await engine.ask("loop forever")
    assert len(turn.steps) == MAX_TOOL_STEPS
    assert turn.reply.startswith("Here's what I found")
    last_prompt = engine.router.states["groq"].provider.requests[-1].user
    assert "Respond to the user now" in last_prompt


async def test_chat_reports_busy_providers_instead_of_failing(settings, sessions, news):
    engine = chat_for(settings, news, sessions, [RateLimited("groq", "TPD", 900)])
    turn = await engine.ask("hello")
    assert turn.error and "rate limits" in turn.reply and "seconds" in turn.reply
    msgs = await engine.messages(turn.conversation_id)
    assert msgs[-1].error is True


async def test_chat_rejects_malformed_steps(settings, sessions, news):
    engine = chat_for(
        settings,
        news,
        sessions,
        ['{"action": "respond", "tool": "", "arguments_json": "", "message": ""}'],
    )
    turn = await engine.ask("hello")
    assert turn.error  # the only provider gave an unusable step


async def test_delete_conversation(settings, sessions, news):
    engine = chat_for(settings, news, sessions, [say("hi")])
    turn = await engine.ask("hello")
    assert await engine.delete(turn.conversation_id)
    assert await engine.messages(turn.conversation_id) is None
    assert not await engine.delete(turn.conversation_id)


# --- API --------------------------------------------------------------------------------------


async def test_chat_api(settings):
    router = router_for(settings, {"groq": [say("Hello from Neurovex.")]})

    async def factory(_s, _h):
        return FakeSummarizer()

    app = create_app(settings, factory, llm=router)
    headers = {"X-Neurovex-Client": "test"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            assert (await c.post("/api/chat", json={"message": "hi"})).status_code == 403
            r = await c.post("/api/chat", json={"message": "hi"}, headers=headers)
            body = r.json()
            assert r.status_code == 200 and body["reply"] == "Hello from Neurovex."
            cid = body["conversation_id"]
            convs = (await c.get("/api/chat/conversations")).json()
            assert convs[0]["id"] == cid and convs[0]["title"] == "hi"
            msgs = (await c.get(f"/api/chat/conversations/{cid}")).json()
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert (await c.get("/api/chat/conversations/999")).status_code == 404
            assert (
                await c.delete(f"/api/chat/conversations/{cid}", headers=headers)
            ).status_code == 204
            assert (
                await c.post("/api/chat", json={"message": ""}, headers=headers)
            ).status_code == 422


def test_signatures_are_compact():
    from neurovex.modules.news.skills import SearchParams

    async def noop(_):
        return SkillResult(ok=True, summary="")

    sig = Skill("news.search", "x", noop, SearchParams).signature()
    assert sig.startswith("news.search(query?: str (Words to look for")
    assert "hours: int = 168" in sig and "{" not in sig  # no raw JSON Schema


async def test_repeated_tool_calls_are_not_rerun(settings, sessions, news):
    engine = chat_for(settings, news, sessions, [
        use("news.top"), use("news.top"), say("Done."),
    ])  # fmt: skip
    turn = await engine.ask("top?")
    assert [s["tool"] for s in turn.steps] == ["news.top"]  # second identical call skipped
    assert "Already done above" in engine.router.states["groq"].provider.requests[2].user


async def test_rate_limit_after_tools_still_answers_from_results(settings, sessions, news):
    engine = chat_for(settings, news, sessions, [use("news.top"), RateLimited("groq", "TPM", 600)])
    turn = await engine.ask("What's important?")
    assert not turn.error and turn.cards  # the article cards are still there
    assert "raw results" in turn.reply and "Top" in turn.reply
