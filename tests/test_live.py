from __future__ import annotations

import asyncio
import json

import pytest

from neurovex.core.live import Broadcaster, format_sse, sse_stream


def test_format_sse():
    assert format_sse("article", {"id": 1}) == 'event: article\ndata: {"id": 1}\n\n'


async def test_broadcast_reaches_subscribers_and_cleans_up():
    b = Broadcaster()
    with b.subscribe() as q1, b.subscribe() as q2:
        assert b.subscriber_count == 2
        b.publish("run", {"state": "started"})
        (e1, d1), (e2, _) = q1.get_nowait(), q2.get_nowait()
        assert e1 == e2 == "run" and d1["state"] == "started" and "ts" in d1
    assert b.subscriber_count == 0


async def test_full_queue_never_blocks_publisher():
    b = Broadcaster()
    with b.subscribe():
        for i in range(1000):
            b.publish("article", {"id": i})  # must not raise


async def test_sse_stream_yields_events_and_heartbeats():
    b = Broadcaster()
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    stream = sse_stream(b, is_disconnected, heartbeat_s=0.05)
    assert "hello" in await anext(stream)
    b.publish("breaking", {"id": 7})
    chunk = await asyncio.wait_for(anext(stream), 1)
    assert chunk.startswith("event: breaking") and json.loads(chunk.split("data: ")[1])["id"] == 7
    assert await asyncio.wait_for(anext(stream), 1) == ": keep-alive\n\n"
    disconnected = True
    await stream.aclose()
    assert b.subscriber_count == 0


async def test_one_stream_merges_several_broadcasters():
    news, tasks = Broadcaster(), Broadcaster()

    async def connected() -> bool:
        return False

    stream = sse_stream([news, tasks], connected, heartbeat_s=30)
    assert "hello" in await anext(stream)
    assert news.subscriber_count == tasks.subscriber_count == 1
    tasks.publish("reminder", {"id": 1})
    news.publish("article", {"id": 2})
    first = await asyncio.wait_for(anext(stream), 1)
    second = await asyncio.wait_for(anext(stream), 1)
    assert first.startswith("event: reminder") and second.startswith("event: article")
    tasks.close()  # either module shutting down ends the merged stream
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 1)
    assert news.subscriber_count == tasks.subscriber_count == 0


async def test_close_ends_open_streams_for_shutdown():
    from neurovex.core.live import close_all

    b = Broadcaster()

    async def connected() -> bool:
        return False

    stream = sse_stream(b, connected, heartbeat_s=30)
    assert "hello" in await anext(stream)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    close_all()  # what the Ctrl+C handler calls
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 1)
    assert b.closed and b.subscriber_count == 0


async def test_guard_passes_streams_through(settings):
    """The guard is plain ASGI: it must not buffer or wrap streaming bodies."""
    from neurovex.api.guard import LocalGuardMiddleware

    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"b", "more_body": False})

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "headers": [(b"host", b"127.0.0.1:8765")]}
    await LocalGuardMiddleware(app)(scope, None, send)
    assert [m.get("body") for m in sent[1:]] == [b"a", b"b"]
