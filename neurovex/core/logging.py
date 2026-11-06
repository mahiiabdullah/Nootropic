"""Structured logging with structlog.

Dev profile renders readable console lines; prod renders JSON. A redaction
processor masks anything that looks like a secret before it is written.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from neurovex.core.config import Profile, Settings

_SECRET_KEY = re.compile(r"(key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,})")


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k, v in list(event_dict.items()):
        if k != "event" and _SECRET_KEY.search(k):
            event_dict[k] = "***"
        elif isinstance(v, str) and _SECRET_VALUE.search(v):
            event_dict[k] = _SECRET_VALUE.sub("***", v)
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # Quiet chatty libraries.
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Page extraction libraries warn on every page they can't parse; that's expected.
    for name in ("trafilatura", "htmldate", "courlan", "justext"):
        logging.getLogger(name).setLevel(logging.ERROR)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.profile is Profile.PROD
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
