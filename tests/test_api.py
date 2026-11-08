from __future__ import annotations

import asyncio
import re

import httpx
import pytest
import respx

from neurovex.main import create_app
from tests.conftest import hours_ago, rss

HEADERS = {"X-Neurovex-Client": "test"}


@pytest.fixture
async def client(settings, fake_summarizer):
    async def factory(_settings, _http):
        return fake_summarizer

    app = create_app(settings, factory)
    with respx.mock(assert_all_called=False) as mock:
        # Every default feed returns the same two stories (deduped by URL).
        mock.get(url__regex=r"https?://.*").respond(
            200,
            content=rss(
                ("AI story", "https://story.test/ai", hours_ago(1)),
                ("Chip story", "https://story.test/chip", hours_ago(2)),
            ),
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                yield c


async def wait_for_idle(client: httpx.AsyncClient) -> dict:
    for _ in range(100):
        st = (await client.get("/api/news/status")).json()
        if not st["running"]:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError("refresh never finished")


async def test_health_and_index(client):
    health = (await client.get("/health")).json()
    assert health["status"] == "ok" and health["modules"]["news"]["provider"] == "fake-model"
    page = await client.get("/")
    assert page.status_code == 200 and "NEUROVEX" in page.text


async def test_page_script_only_uses_elements_that_exist(client):
    """The page is one file: moving markup around must not orphan the script that drives it."""
    page = (await client.get("/")).text
    script = page[page.index('"use strict"') :]
    defined = set(re.findall(r'\bid="([\w-]+)"', page)) | set(re.findall(r'\bid: "([\w-]+)"', page))
    missing = sorted(set(re.findall(r'\$\("([\w-]+)"\)', script)) - defined)
    assert not missing, f"Script uses ids the page doesn't define: {missing}"
    # Every section in the rail has a view, and the theme is set before the page paints.
    sections = re.findall(r'data-section="(\w+)"', page)
    assert sections == ["today", "news", "tasks", "ask", "memory", "settings"]
    assert page.index("nvx.theme") < page.index("<style>")


async def test_guard_blocks_foreign_host_and_headerless_writes(client):
    r = await client.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 403
    r = await client.post("/api/news/refresh")
    assert r.status_code == 403 and "X-Neurovex-Client" in r.json()["detail"]


async def test_page_is_compressed_and_reused_when_unchanged(client):
    first = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert first.headers["content-encoding"] == "gzip" and first.headers["etag"]
    again = await client.get("/", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


async def test_allowed_hosts_open_the_page_at_another_address(settings, fake_summarizer):
    async def factory(_settings, _http):
        return fake_summarizer

    listed = settings.model_copy(update={"allowed_hosts": "PC.Tail1234.ts.net, other.lan:8443"})
    assert listed.extra_hosts() == {"pc.tail1234.ts.net", "other.lan"}
    app = create_app(listed, factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            ok = await c.get("/health", headers={"Host": "pc.tail1234.ts.net"})
            assert ok.status_code == 200
            blocked = await c.get("/health", headers={"Host": "evil.example"})
            assert blocked.status_code == 403
            assert "evil.example" in blocked.json()["detail"]
            assert "NEUROVEX_ALLOWED_HOSTS" in blocked.json()["detail"]
            # Writes still need the client header, whatever the address.
            write = await c.post("/api/news/refresh", headers={"Host": "pc.tail1234.ts.net"})
            assert write.status_code == 403


async def test_refresh_then_browse_bookmark_and_read(client):
    assert (await client.post("/api/news/refresh", headers=HEADERS)).status_code == 202
    st = await wait_for_idle(client)
    assert st["last_run"]["status"] == "OK"
    assert st["counts"]["PROCESSED"] == 2

    page = (await client.get("/api/news/articles", params={"sort": "importance"})).json()
    assert page["total"] == 2
    article = page["items"][0]
    assert article["summary"] and article["source_name"]

    r = await client.post(
        f"/api/news/articles/{article['id']}/bookmark", json={"value": True}, headers=HEADERS
    )
    assert r.json()["bookmarked"] is True
    r = await client.post(
        f"/api/news/articles/{article['id']}/read", json={"value": True}, headers=HEADERS
    )
    assert r.json()["read"] is True

    saved = (await client.get("/api/news/articles", params={"bookmarked": True})).json()
    assert [a["id"] for a in saved["items"]] == [article["id"]]
    unread = (await client.get("/api/news/articles", params={"unread": True})).json()
    assert article["id"] not in [a["id"] for a in unread["items"]]

    cats = {c["name"]: c["count"] for c in (await client.get("/api/news/categories")).json()}
    assert cats["AI"] == 1
    digest = (await client.get("/api/news/digest")).json()
    assert len(digest) == 2
    assert (await client.get("/api/news/articles/999999")).status_code == 404


async def test_sources_crud(client):
    sources = (await client.get("/api/news/sources")).json()
    assert len(sources) >= 5  # defaults seeded on first start

    bad = await client.post(
        "/api/news/sources", json={"name": "x", "url": "file:///etc/passwd"}, headers=HEADERS
    )
    assert bad.status_code == 422
    r = await client.post(
        "/api/news/sources",
        json={"name": "Mine", "url": "https://mine.test/feed.xml"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    dupe = await client.post(
        "/api/news/sources",
        json={"name": "Mine", "url": "https://mine.test/feed.xml"},
        headers=HEADERS,
    )
    assert dupe.status_code == 409

    r = await client.patch(f"/api/news/sources/{sid}", json={"enabled": False}, headers=HEADERS)
    assert r.json()["enabled"] is False
    assert (await client.delete(f"/api/news/sources/{sid}", headers=HEADERS)).status_code == 204
    assert sid not in [s["id"] for s in (await client.get("/api/news/sources")).json()]


async def test_deleting_source_cascades_to_articles(client):
    await client.post("/api/news/refresh", headers=HEADERS)
    await wait_for_idle(client)
    sources = (await client.get("/api/news/sources")).json()
    owner = next(s for s in sources if s["article_count"])
    assert (
        await client.delete(f"/api/news/sources/{owner['id']}", headers=HEADERS)
    ).status_code == 204
    left = (await client.get("/api/news/articles", params={"source_id": owner["id"]})).json()
    assert left["total"] == 0


async def test_personal_flow_follow_feedback_profile_and_briefing(client):
    await client.post("/api/news/refresh", headers=HEADERS)
    await wait_for_idle(client)

    r = await client.post(
        "/api/news/interests", json={"value": "AI", "kind": "category"}, headers=HEADERS
    )
    assert r.status_code == 201 and r.json()["mode"] == "follow"
    feed = (await client.get("/api/news/articles")).json()["items"]
    assert feed[0]["title"] == "AI story" and "You follow AI" in feed[0]["reasons"]

    chip = next(a for a in feed if a["title"] == "Chip story")
    r = await client.post(
        f"/api/news/articles/{chip['id']}/feedback", json={"action": "more"}, headers=HEADERS
    )
    assert r.status_code == 200
    profile = (await client.get("/api/news/profile")).json()
    assert profile["interactions"] == 1 and profile["learned_likes"]
    assert [i["label"] for i in profile["following"]] == ["AI"]

    r = await client.post(
        f"/api/news/articles/{chip['id']}/feedback", json={"action": "hide"}, headers=HEADERS
    )
    titles = [a["title"] for a in (await client.get("/api/news/articles")).json()["items"]]
    assert "Chip story" not in titles
    await client.post(f"/api/news/articles/{chip['id']}/unhide", headers=HEADERS)
    titles = [a["title"] for a in (await client.get("/api/news/articles")).json()["items"]]
    assert "Chip story" in titles

    bad = await client.post(
        "/api/news/interests", json={"value": "Nope", "kind": "category"}, headers=HEADERS
    )
    assert bad.status_code == 422
    iid = profile["following"][0]["id"]
    assert (await client.delete(f"/api/news/interests/{iid}", headers=HEADERS)).status_code == 204
    reset = await client.post("/api/news/profile/reset-learning", headers=HEADERS)
    assert reset.json()["removed"] == 2


async def test_seen_tracks_new_since_last_visit(client):
    st = (await client.get("/api/news/status")).json()
    assert st["last_seen_at"] is None and st["new_since_seen"] == 0
    assert (await client.post("/api/news/seen", headers=HEADERS)).status_code == 204
    await client.post("/api/news/refresh", headers=HEADERS)
    st = await wait_for_idle(client)
    assert st["last_seen_at"] is not None and st["new_since_seen"] == 2
    items = (await client.get("/api/news/articles")).json()["items"]
    assert all(a["is_new"] for a in items)


async def test_briefing_endpoints(client):
    assert (await client.get("/api/news/briefing")).json() is None
    await client.post("/api/news/refresh", headers=HEADERS)
    await wait_for_idle(client)
    # Only two stories exist, so no auto briefing and a manual one fails gracefully.
    assert (await client.post("/api/news/briefing", headers=HEADERS)).status_code == 202
    for _ in range(50):
        if not (await client.get("/api/news/status")).json()["briefing_running"]:
            break
        await asyncio.sleep(0.05)
    assert (await client.get("/api/news/briefing")).json() is None


async def test_catalog_marks_added_feeds(client):
    groups = (await client.get("/api/news/catalog")).json()
    feeds = {f["url"]: f["added"] for g in groups for f in g["feeds"]}
    assert feeds["https://hnrss.org/frontpage"] is True  # a default source
    assert feeds["https://www.cisa.gov/cybersecurity-advisories/all.xml"] is False


async def test_llm_endpoints_without_keys(client):
    data = (await client.get("/api/llm/providers")).json()
    assert data["order"] == ["puku"] and data["active"] == "none"
    assert [p["id"] for p in data["providers"]] == ["puku"]
    assert all(p["status"] == "not_configured" for p in data["providers"])
    assert (await client.post("/api/llm/test", headers=HEADERS)).json() == []
    r = await client.post("/api/llm/test", params={"provider": "nope"}, headers=HEADERS)
    assert r.status_code == 404
    assert (await client.get("/api/llm/models/groq")).status_code == 404
