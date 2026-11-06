"""The user's local time.

Data is stored in UTC; "today", "tomorrow 9am" and the time shown to the model are
the user's. Set `NEUROVEX_TIMEZONE` (an IANA name such as "Asia/Dhaka") on servers,
which usually run in UTC. Without it, this computer's current UTC offset is used.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo


def local_tz(name: str = "") -> tzinfo:
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def local_now(name: str = "") -> datetime:
    return datetime.now(local_tz(name))
