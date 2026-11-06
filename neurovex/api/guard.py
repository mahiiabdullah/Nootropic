"""Protection for the localhost API until token auth lands (docs/PROMPTS.md P0.3).

Any website you visit can send requests to http://127.0.0.1:<port>. Two checks
stop that from being abused:

1. Host header must be a loopback name. This blocks DNS-rebinding attacks.
2. State-changing requests must carry `X-Neurovex-Client`. Browsers can't add a
   custom header cross-origin without a CORS preflight, and this app grants no
   CORS, so a hostile page can't trigger refreshes or edit data.

Written as plain ASGI middleware (not Starlette's BaseHTTPMiddleware) so it never
wraps response bodies: long-lived streams like the live-events SSE pass straight
through and shut down cleanly.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

CLIENT_HEADER = "X-Neurovex-Client"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LocalGuardMiddleware:
    def __init__(self, app: ASGIApp, allow_remote: bool = False) -> None:
        self.app = app
        self.allow_remote = allow_remote

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not self.allow_remote:
            host = (headers.get("host") or "").rsplit(":", 1)[0].lower()
            if host not in LOOPBACK_HOSTS:
                await JSONResponse({"detail": "Host not allowed"}, status_code=403)(
                    scope, receive, send
                )
                return
        if scope["method"] not in SAFE_METHODS and not headers.get(CLIENT_HEADER):
            await JSONResponse({"detail": f"Missing {CLIENT_HEADER} header"}, status_code=403)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)
