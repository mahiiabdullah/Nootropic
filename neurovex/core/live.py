"""Live updates: an in-process broadcaster streamed to browsers as Server-Sent Events.

Each module owns one broadcaster. News: `run` (started/finished), `article` (newly
summarized), `breaking`, `briefing`. Tasks: `task` (added/changed), `reminder`.
Today: `today` (the day's brief is ready).
This is the seed of the Neurovex event bus (docs/ARCHITECTURE.md §8).

Streams are long-lived, so the server must be able to end them: `close()` (or
`close_all()` from the Ctrl+C handler) makes every open stream finish at once,
which lets shutdown complete instead of waiting on browser connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from neurovex.core.db import utcnow

HEARTBEAT_S = 15.0
QUEUE_SIZE = 200
_CLOSE = "__close__"

_ALL: weakref.WeakSet[Broadcaster] = weakref.WeakSet()


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.closed = False
        _ALL.add(self)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = {**data, "ts": utcnow().isoformat()}
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):  # a stuck client must not block us
                q.put_nowait((event, payload))

    def close(self) -> None:
        """End every open stream now (server shutdown)."""
        self.closed = True
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):  # a full queue drains fast anyway
                q.put_nowait((_CLOSE, {}))

    def close_threadsafe(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.close)
        else:
            self.closed = True

    @contextlib.contextmanager
    def subscribe(self, q: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None):  # type: ignore[no-untyped-def]
        """Receive events on `q` (a new queue by default; pass one to merge broadcasters)."""
        self._loop = asyncio.get_running_loop()
        if q is None:
            q = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)


def close_all() -> None:
    """Close every broadcaster in this process. Safe to call from a signal handler."""
    for b in list(_ALL):
        b.close_threadsafe()


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def sse_stream(
    source: Broadcaster | Sequence[Broadcaster],
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_s: float = HEARTBEAT_S,
) -> AsyncIterator[str]:
    """One SSE stream from one broadcaster or several merged (one connection per page:
    browsers allow only ~6 per site, so every extra stream per tab counts)."""
    broadcasters = [source] if isinstance(source, Broadcaster) else list(source)
    q: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=QUEUE_SIZE)
    with contextlib.ExitStack() as stack:
        for b in broadcasters:
            stack.enter_context(b.subscribe(q))
        yield format_sse("hello", {"ok": True})
        while not any(b.closed for b in broadcasters) and not await is_disconnected():
            try:
                event, data = await asyncio.wait_for(q.get(), timeout=heartbeat_s)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event == _CLOSE:
                break
            yield format_sse(event, data)
