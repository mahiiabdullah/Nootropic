"""Sign-in for the API and the web page (see `neurovex/security/auth.py` for the design).

Everything needs a signed-in browser (session cookie) or the API token
(`Authorization: Bearer …`), except what a signed-out page needs to show its sign-in
screen: the page itself, /health, and the sign-in endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from neurovex.security.auth import COOKIE, AuthService, TooManyAttempts, device_label

OPEN_PATHS = {"/", "/health", "/signin", "/api/auth/login", "/api/auth/status", "/favicon.ico"}
COOKIE_MAX_AGE = 400 * 86400  # the browser's limit; the server signs idle devices out sooner


def _bearer(conn: HTTPConnection) -> str:
    value = conn.headers.get("authorization", "")
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


async def is_signed_in(auth: AuthService, conn: HTTPConnection) -> bool:
    if token := _bearer(conn):
        return auth.check_token(token)
    return await auth.check_session(conn.cookies.get(COOKIE, ""))


class AuthMiddleware:
    """Plain ASGI (like the guard), so the live-event stream passes straight through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        auth: AuthService = scope["app"].state.auth
        if not await is_signed_in(auth, HTTPConnection(scope)):
            response = JSONResponse({"detail": "Sign in required"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    secret: str = Field(min_length=1, max_length=200)  # a one-time code or the API token


def _auth(request: Request) -> AuthService:
    return request.app.state.auth


def _set_cookie(request: Request, response: Response, cookie: str) -> None:
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE, cookie, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict", secure=https,
        path="/",
    )  # fmt: skip


async def _sign_in(request: Request, secret: str) -> str | None:
    auth = _auth(request)
    try:
        result = await auth.sign_in(secret, device_label(request.headers.get("user-agent", "")))
    except TooManyAttempts:
        await request.app.state.audit.append("user", "auth.login", outcome="locked")
        raise HTTPException(429, "Too many wrong tries. Wait 10 minutes and try again.") from None
    if result is None:
        await request.app.state.audit.append("user", "auth.login", outcome="wrong")
        return None
    cookie, how = result
    await request.app.state.audit.append("user", "auth.login", {"with": how}, outcome="ok")
    return cookie


@router.get("/api/auth/status")
async def status(request: Request) -> dict[str, bool]:
    return {"signed_in": await is_signed_in(_auth(request), HTTPConnection(request.scope))}


@router.post("/api/auth/login")
async def login(request: Request, body: LoginIn, response: Response) -> dict[str, bool]:
    cookie = await _sign_in(request, body.secret)
    if cookie is None:
        raise HTTPException(401, "That code or token didn't work. Codes work once, for 10 minutes.")
    _set_cookie(request, response, cookie)
    return {"signed_in": True}


@router.get("/signin", include_in_schema=False)
async def signin_link(request: Request, code: str = "") -> RedirectResponse:
    """The link `nvx signin` prints: sign this browser in, then open the app."""
    try:
        cookie = await _sign_in(request, code) if code else None
    except HTTPException:
        cookie = None
    if cookie is None:
        return RedirectResponse("/?signin=failed", status_code=303)
    response = RedirectResponse("/", status_code=303)
    _set_cookie(request, response, cookie)
    return response


@router.post("/api/auth/code")
async def new_code(request: Request) -> dict[str, str]:
    """A one-time code to sign in another device (shown in Settings)."""
    made = await _auth(request).create_code()
    await request.app.state.audit.append("user", "auth.code_created")
    return {"code": made.code, "expires_at": made.expires_at.isoformat()}


@router.get("/api/auth/sessions")
async def sessions(request: Request) -> list[dict[str, object]]:
    auth = _auth(request)
    current = await auth.session_id(request.cookies.get(COOKIE, ""))
    return [
        {"id": s.id, "label": s.label, "created_at": s.created_at.isoformat(),
         "last_seen_at": s.last_seen_at.isoformat(), "current": s.id == current}
        for s in await auth.list_sessions()
    ]  # fmt: skip


@router.post("/api/auth/sessions/{session_id}/signout")
async def sign_out(request: Request, session_id: int, response: Response) -> dict[str, bool]:
    auth = _auth(request)
    current = await auth.session_id(request.cookies.get(COOKIE, ""))
    if not await auth.sign_out(session_id):
        raise HTTPException(404, "No such device")
    await request.app.state.audit.append("user", "auth.signout", {"session": session_id})
    if session_id == current:
        response.delete_cookie(COOKIE, path="/")
    return {"signed_out": True}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    """Sign this browser out."""
    auth = _auth(request)
    if (current := await auth.session_id(request.cookies.get(COOKIE, ""))) is not None:
        await auth.sign_out(current)
        await request.app.state.audit.append("user", "auth.signout", {"session": current})
    response.delete_cookie(COOKIE, path="/")
    return {"signed_out": True}
