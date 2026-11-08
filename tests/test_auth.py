"""Sign-in: what's open, what isn't, codes, browser sessions, signing out, brute force."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import update

from neurovex.core.db import utcnow
from neurovex.main import create_app
from neurovex.security import auth as auth_mod
from neurovex.security.auth import (
    COOKIE,
    device_label,
    format_code,
    load_api_token,
    normalize_code,
    store_api_token,
)
from neurovex.security.models import AuthCode
from tests.conftest import rss, signed_in

WEB = {"X-Neurovex-Client": "web"}
CHROME_ANDROID = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/141.0 Mobile"


@pytest.fixture
async def app(settings, fake_summarizer):
    async def factory(_settings, _http):
        return fake_summarizer

    app = create_app(settings, factory)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https?://.*").respond(200, content=rss())
        async with app.router.lifespan_context(app):
            yield app


def browser(app, **headers) -> httpx.AsyncClient:
    """A fresh browser: no cookie, no token."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=headers)


async def test_everything_needs_sign_in_except_the_sign_in_screen(app):
    async with browser(app) as b:
        for path in ("/api/news/articles", "/api/tasks", "/api/memory", "/api/chat/conversations",
                     "/api/llm/providers", "/docs", "/openapi.json", "/api/auth/sessions"):  # fmt: skip
            r = await b.get(path)
            assert r.status_code == 401, path
            assert r.json()["detail"] == "Sign in required"
        assert (await b.get("/")).status_code == 200  # the page, which shows the sign-in screen
        assert (await b.get("/health")).status_code == 200
        assert (await b.get("/api/auth/status")).json() == {"signed_in": False}
        assert (await b.post("/api/tasks", json={"text": "x"}, headers=WEB)).status_code == 401


async def test_scripts_use_the_api_token(app, settings):
    async with browser(app, **signed_in(settings)) as b:
        assert (await b.get("/api/news/articles")).status_code == 200
    async with browser(app, Authorization="Bearer not-the-token") as b:
        assert (await b.get("/api/news/articles")).status_code == 401


async def test_a_browser_signs_in_with_the_token_and_stays_signed_in(app, settings):
    async with browser(app, **{"User-Agent": CHROME_ANDROID}) as b:
        r = await b.post("/api/auth/login", json={"secret": load_api_token(settings)}, headers=WEB)
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age" in cookie
        assert (await b.get("/api/auth/status")).json() == {"signed_in": True}
        assert (await b.get("/api/news/articles")).status_code == 200
        # Writes still need the client header: a cookie alone can't be used from another site.
        assert (await b.post("/api/tasks", json={"text": "Call mum"})).status_code == 403
        devices = (await b.get("/api/auth/sessions")).json()
        assert [(d["label"], d["current"]) for d in devices] == [("Chrome on Android", True)]


async def test_a_one_time_code_signs_in_a_phone_once(app, settings):
    async with browser(app, **signed_in(settings)) as pc:
        made = (await pc.post("/api/auth/code", headers=WEB)).json()
    assert normalize_code(made["code"]) is not None and "-" in made["code"]
    async with browser(app) as phone:
        typed = made["code"].lower().replace("-", " ")  # however it's typed
        assert (await phone.post("/api/auth/login", json={"secret": typed}, headers=WEB)).is_success
        assert (await phone.get("/api/tasks")).status_code == 200
    async with browser(app) as again:
        r = await again.post("/api/auth/login", json={"secret": made["code"]}, headers=WEB)
        assert r.status_code == 401 and "work once" in r.json()["detail"]


async def test_codes_expire(app, settings):
    made = await app.state.auth.create_code()
    async with app.state.auth.sessions() as s:
        await s.execute(update(AuthCode).values(expires_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    async with browser(app) as b:
        r = await b.post("/api/auth/login", json={"secret": made.code}, headers=WEB)
        assert r.status_code == 401


async def test_the_link_nvx_signin_prints(app):
    made = await app.state.auth.create_code()
    async with browser(app) as b:
        r = await b.get(f"/signin?code={normalize_code(made.code)}")
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert COOKIE in r.headers["set-cookie"]
        assert (await b.get("/api/auth/status")).json() == {"signed_in": True}
        used = await b.get(f"/signin?code={normalize_code(made.code)}")
        assert used.headers["location"] == "/?signin=failed"


async def test_signing_a_device_out(app, settings):
    async with browser(app) as phone:
        code = (await app.state.auth.create_code()).code
        await phone.post("/api/auth/login", json={"secret": code}, headers=WEB)
        async with browser(app, **signed_in(settings)) as pc:
            devices = (await pc.get("/api/auth/sessions")).json()
            r = await pc.post(f"/api/auth/sessions/{devices[0]['id']}/signout", headers=WEB)
            assert r.status_code == 200
        assert (await phone.get("/api/tasks")).status_code == 401  # at once, not after a cache
    async with browser(app) as b:
        await b.post("/api/auth/login", json={"secret": load_api_token(settings)}, headers=WEB)
        assert (await b.post("/api/auth/logout", headers=WEB)).status_code == 200
        assert (await b.get("/api/auth/status")).json() == {"signed_in": False}


async def test_wrong_guesses_pause_sign_in(app, settings):
    async with browser(app) as b:
        for _ in range(auth_mod.MAX_FAILURES):
            r = await b.post("/api/auth/login", json={"secret": "AAAA-AAAA"}, headers=WEB)
            assert r.status_code == 401
        token = load_api_token(settings)
        r = await b.post("/api/auth/login", json={"secret": token}, headers=WEB)
        assert r.status_code == 429  # even the right one, until the window passes
    entries = await app.state.audit.tail(50)
    outcomes = [e.outcome for e in entries if e.action == "auth.login"]
    assert outcomes.count("wrong") == auth_mod.MAX_FAILURES and outcomes[-1] == "locked"


async def test_idle_devices_must_sign_in_again(app, settings):
    async with browser(app) as b:
        await b.post("/api/auth/login", json={"secret": load_api_token(settings)}, headers=WEB)
        app.state.auth._valid.clear()  # past the short in-memory cache
        from neurovex.security.models import AuthSession

        async with app.state.auth.sessions() as s:
            old = utcnow() - auth_mod.SESSION_IDLE - timedelta(days=1)
            await s.execute(update(AuthSession).values(last_seen_at=old))
            await s.commit()
        assert (await b.get("/api/tasks")).status_code == 401


def test_the_token_is_made_once_and_kept(settings):
    first = load_api_token(settings)
    assert len(first) >= 40 and load_api_token(settings) == first
    assert (settings.data_dir / "api_token").read_text().strip() == first  # tests: a file
    store_api_token(settings, "rotated-token-value")
    assert load_api_token(settings) == "rotated-token-value"


def test_codes_and_labels():
    assert normalize_code("k7qm-4xpa") == "K7QM4XPA" and format_code("K7QM4XPA") == "K7QM-4XPA"
    assert normalize_code("K7QM-4XP0") is None  # 0 isn't in the alphabet: it's not a code
    assert normalize_code("x" * 43) is None  # a token, not a code
    assert device_label(CHROME_ANDROID) == "Chrome on Android"
    edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0 Safari/537.36 Edg/141.0"
    assert device_label(edge) == "Edge on Windows"
    assert device_label("") == "Browser"
