"""Uvicorn server with a clean Ctrl+C: open live-event streams are closed first.

Without this, uvicorn waits for every connection to finish before shutting down,
and a browser's SSE connection never finishes on its own, so Ctrl+C hangs and a
second Ctrl+C force-cancels requests (noisy tracebacks).
"""

from __future__ import annotations

import contextlib
from types import FrameType

import uvicorn

from neurovex.core.live import close_all

GRACEFUL_SHUTDOWN_S = 5  # backstop for any other slow request


class NeurovexServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        close_all()
        super().handle_exit(sig, frame)


def run(host: str, port: int, reload: bool = False) -> None:
    if reload:  # the reloader supervises worker processes; keep uvicorn's own runner
        uvicorn.run(
            "neurovex.main:create_app", factory=True, host=host, port=port, reload=True,
            log_level="warning", timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        )  # fmt: skip
        return
    config = uvicorn.Config(
        "neurovex.main:create_app", factory=True, host=host, port=port,
        log_level="warning", timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )  # fmt: skip
    # uvicorn re-raises the captured Ctrl+C after a clean shutdown; uvicorn.run()
    # swallows it the same way.
    with contextlib.suppress(KeyboardInterrupt):
        NeurovexServer(config).run()
