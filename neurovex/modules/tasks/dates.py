"""Natural dates and quick-add for tasks: "Call bank tomorrow 10am !high #errands ~15m".

Deterministic and local (no AI). Quick-add in the web UI and CLI and the `due`
argument of chat skills all go through here. Conservative on purpose: words only
count as a date when they clearly are one, so "Read chapter 5" keeps its 5,
"Prepare Friday's slides" keeps its Friday and "Morning run" stays a title.

All-day tasks are stored as the end of that local day (23:59:59), so "overdue"
is simply `due_at < now` for every task.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo

END_OF_DAY = time(23, 59, 59)
REPEATS = ("daily", "weekdays", "weekly", "monthly")
PRIORITIES = {"low": 1, "normal": 2, "high": 3, "urgent": 4}

WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}  # fmt: skip
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_AMBIGUOUS_DAYS = {"sun", "sat", "wed"}  # also English words: need "on", "by", "next"…
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "ten": 10}  # fmt: skip
_PART_OF_DAY = {"morning": time(9), "noon": time(12), "midday": time(12),
                "afternoon": time(15), "evening": time(19), "tonight": time(20)}  # fmt: skip
_SAFE_PARTS = {"noon", "midday", "tonight"}  # these are times even without "in the"


def _alt(words: dict[str, object]) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_WD, _MO = _alt(WEEKDAYS), _alt(MONTHS)
_PRE = r"(?:\b(?:due|by|on|for|until)\s+)?"
_NOT_POSSESSIVE = r"(?!['’]\w)"

_REPEAT = re.compile(
    rf"\b(?:every\s+(?P<every>day|weekday|week|month|{_WD})|(?P<adv>daily|weekdays|weekly|monthly))"
    rf"\b{_NOT_POSSESSIVE}",
    re.I,
)
_ISO = re.compile(
    rf"{_PRE}\b(?P<y>\d{{4}})-(?P<m>\d{{1,2}})-(?P<d>\d{{1,2}})"
    r"(?:[ T](?P<H>[01]?\d|2[0-3]):(?P<M>[0-5]\d))?\b",
    re.I,
)
_REL_DAY = re.compile(
    rf"{_PRE}\b(?P<w>day\s+after\s+tomorrow|today|tonight|tomorrow|tmrw|tmr)\b{_NOT_POSSESSIVE}",
    re.I,
)
_IN = re.compile(
    rf"\bin\s+(?P<n>\d{{1,3}}|{_alt(_NUMBERS)})\s+"
    r"(?P<u>minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b",
    re.I,
)
_SPAN = re.compile(
    rf"{_PRE}\b(?P<w>next\s+week|next\s+month|this\s+weekend|weekend|(?:later\s+)?this\s+week"
    r"|this\s+month|end\s+of\s+(?:the\s+)?(?:week|month))\b",
    re.I,
)
_MONTH_DAY = re.compile(
    r"(?:\b(?P<pre>due|by|on|for|until)\s+)?"
    rf"\b(?:(?P<mo1>{_MO})\.?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"|(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mo2>{_MO})\.?)"
    r"(?:,?\s+(?P<y>\d{4}))?\b",
    re.I,
)
_WEEKDAY = re.compile(  # no "for": "Study for SAT" is not a Saturday task
    r"(?:\b(?P<pre>due|by|on|until)\s+)?\b(?:(?P<rel>next|this|coming)\s+)?"
    rf"(?P<wd>{_WD})\b{_NOT_POSSESSIVE}",
    re.I,
)
_TIME_AMPM = re.compile(
    r"\b(?:at\s+)?(?P<h>1[0-2]|0?[1-9])(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap])\.?m\.?(?!\w)", re.I
)
_TIME_24 = re.compile(r"\b(?:at\s+)?(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)\b")
_TIME_AT = re.compile(r"\bat\s+(?P<h>1[0-9]|2[0-3]|0?[1-9])\b(?![:.]\d)", re.I)
_TIME_WORD = re.compile(
    rf"\b(?:(?P<q>in\s+the|this|at)\s+)?(?P<w>{_alt(_PART_OF_DAY)})\b{_NOT_POSSESSIVE}", re.I
)
_PRIORITY = re.compile(r"(?:^|\s)!(?P<word>low|normal|high|urgent|!{0,2})(?=\s|$)", re.I)
_TAG = re.compile(r"(?:^|\s)#(?P<t>[^\W\d][\w-]{0,29})(?=\s|$)")
_ESTIMATE = re.compile(
    r"(?:^|\s)~\s?(?:(?P<h>\d{1,2})\s?h(?:rs?|ours?)?)?\s?(?:(?P<m>\d{1,3})\s?m(?:ins?)?)?(?=\s|$)",
    re.I,
)
_LEFTOVER = re.compile(r"(?:\s+(?:by|on|at|due|for|until|every|in|the|-|–|,))+\s*$", re.I)


@dataclass(frozen=True)
class Due:
    at: datetime  # aware, UTC
    all_day: bool


@dataclass
class Parsed:
    title: str
    due: Due | None = None
    priority: int | None = None
    tags: list[str] = field(default_factory=list)
    repeat: str | None = None
    estimate_min: int | None = None


def day_end(d: date, tz: tzinfo) -> datetime:
    return datetime.combine(d, END_OF_DAY, tz).astimezone(UTC)


def at_time(d: date, t: time, tz: tzinfo) -> datetime:
    return datetime.combine(d, t, tz).astimezone(UTC)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


class _Text:
    """The text being parsed; each recognized piece is cut out of the title."""

    def __init__(self, text: str) -> None:
        self.text = f" {text} "

    def take(
        self, pattern: re.Pattern[str], accept: Callable[[re.Match[str]], bool] | None = None
    ) -> re.Match[str] | None:
        for m in pattern.finditer(self.text):
            if accept is None or accept(m):
                self.text = self.text[: m.start()] + " " + self.text[m.end() :]
                return m
        return None

    def take_all(self, pattern: re.Pattern[str]) -> list[re.Match[str]]:
        found = []
        while m := self.take(pattern):
            found.append(m)
        return found


def _number(raw: str) -> int:
    return int(raw) if raw.isdigit() else _NUMBERS[raw.lower()]


def _take_date(
    t: _Text, today: date, now: datetime
) -> tuple[date | None, datetime | None, time | None]:
    """(date, exact moment, default time). "in 2 hours" is an exact moment; "tonight"
    is today with 20:00 unless a time is given."""
    if m := t.take(_ISO):
        try:
            d = date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            return None, None, None
        if m["H"]:
            return d, datetime.combine(d, time(int(m["H"]), int(m["M"])), now.tzinfo), None
        return d, None, None
    if m := t.take(_REL_DAY):
        w = " ".join(m["w"].lower().split())
        if w == "day after tomorrow":
            return today + timedelta(days=2), None, None
        if w == "tonight":
            return today, None, _PART_OF_DAY["tonight"]
        return today + timedelta(days=0 if w == "today" else 1), None, None
    if m := t.take(_IN):
        n, unit = _number(m["n"]), m["u"].lower()
        if unit.startswith(("min", "h")):
            delta = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n)
            moment = (now + delta).replace(second=0, microsecond=0)
            return moment.date(), moment, None
        if unit.startswith("month"):
            return add_months(today, n), None, None
        return today + timedelta(days=n * (7 if unit.startswith("week") else 1)), None, None
    if m := t.take(_SPAN):
        w = " ".join(m["w"].lower().split())
        if w == "next week":
            return today + timedelta(days=7 - today.weekday()), None, None
        if w == "next month":
            return add_months(today.replace(day=1), 1), None, None
        if w.endswith("weekend"):
            return today + timedelta(days=(5 - today.weekday()) % 7), None, None  # Saturday
        if w.endswith("month"):  # this month / end of the month: its last day
            return add_months(today.replace(day=1), 1) - timedelta(days=1), None, None
        wd = today.weekday()  # this week / end of the week: Friday (Sunday once it's passed)
        return today + timedelta(days=4 - wd if wd <= 4 else 6 - wd), None, None
    # "I may 2…" is not May 2nd: the month "may" needs "on/by…" or a year.
    if m := t.take(
        _MONTH_DAY, lambda m: bool(m["pre"] or m["y"]) or (m["mo1"] or m["mo2"]).lower() != "may"
    ):
        month = MONTHS[(m["mo1"] or m["mo2"]).lower()]
        day = int(m["d1"] or m["d2"])
        try:
            d = date(int(m["y"]) if m["y"] else today.year, month, day)
        except ValueError:
            return None, None, None
        if not m["y"] and d < today:
            d = d.replace(year=d.year + 1)
        return d, None, None
    if m := t.take(
        _WEEKDAY,
        lambda m: bool(m["pre"] or m["rel"]) or m["wd"].lower() not in _AMBIGUOUS_DAYS,
    ):
        ahead = (WEEKDAYS[m["wd"].lower()] - today.weekday()) % 7
        if ahead == 0 and (m["rel"] or "").lower() != "this":
            ahead = 7
        return today + timedelta(days=ahead), None, None
    return None, None, None


def _take_time(t: _Text, has_day: bool) -> time | None:
    if m := t.take(_TIME_AMPM):
        h = int(m["h"]) % 12 + (12 if m["ap"].lower() == "p" else 0)
        return time(h, int(m["m"] or 0))
    if m := t.take(_TIME_24):
        return time(int(m["h"]), int(m["m"]))
    if m := t.take(_TIME_AT):
        h = int(m["h"])
        return time(h + 12 if 1 <= h <= 7 else h)  # "at 5" means 5 pm, "at 9" 9 am
    # "Morning run" is a title; "tomorrow morning" or "in the morning" is a time.
    if m := t.take(_TIME_WORD, lambda m: has_day or bool(m["q"]) or m["w"].lower() in _SAFE_PARTS):
        return _PART_OF_DAY[m["w"].lower()]
    return None


def _repeat_of(m: re.Match[str]) -> tuple[str, int | None]:
    """(repeat kind, weekday it's anchored to)."""
    word = (m["every"] or m["adv"]).lower()
    if word in WEEKDAYS:
        return "weekly", WEEKDAYS[word]
    kinds = {"day": "daily", "weekday": "weekdays", "week": "weekly", "month": "monthly"}
    return kinds.get(word, word), None


def first_due(repeat: str, today: date, weekday: int | None = None) -> date:
    """Where a repeating task with no explicit date starts."""
    if weekday is not None:
        return today + timedelta(days=(weekday - today.weekday()) % 7)
    if repeat == "weekdays" and today.weekday() >= 5:
        return today + timedelta(days=7 - today.weekday())
    return today


def parse_quick(text: str, now: datetime) -> Parsed:
    """Split quick-add text into a title and task fields. `now` is aware, in the user's zone."""
    tz = now.tzinfo or UTC
    today = now.date()
    t = _Text(" ".join((text or "").split()))

    repeat, anchor = None, None
    if m := t.take(_REPEAT):
        repeat, anchor = _repeat_of(m)
    day, moment, default_clock = _take_date(t, today, now)
    clock = None if moment else (_take_time(t, has_day=day is not None) or default_clock)

    due = None
    if moment is not None:
        due = Due(moment.astimezone(UTC), all_day=False)
    elif day is not None:
        due = Due(at_time(day, clock, tz), False) if clock else Due(day_end(day, tz), True)
    elif clock is not None:
        start = datetime.combine(today, clock, tz)
        if repeat is not None:
            start = datetime.combine(first_due(repeat, today, anchor), clock, tz)
            if start <= now:  # today's slot has passed: start with the next one
                start = next_due(start, repeat, tz, now)
        elif start <= now:
            start += timedelta(days=1)
        due = Due(start.astimezone(UTC), all_day=False)
    elif repeat is not None:
        due = Due(day_end(first_due(repeat, today, anchor), tz), all_day=True)

    priority = None
    for m in t.take_all(_PRIORITY):
        word = m["word"].lower()
        priority = PRIORITIES.get(word) or (4 if word.startswith("!") else 3)
    tags = list(dict.fromkeys(m["t"].lower() for m in t.take_all(_TAG)))
    estimate = None
    for m in t.take_all(_ESTIMATE):
        if m["h"] or m["m"]:
            estimate = int(m["h"] or 0) * 60 + int(m["m"] or 0) or None

    title = _LEFTOVER.sub("", " ".join(t.text.split())).strip(" -–,")
    return Parsed(
        title=title or " ".join((text or "").split()),
        due=due,
        priority=priority,
        tags=tags,
        repeat=repeat,
        estimate_min=estimate,
    )


def parse_due(text: str, now: datetime) -> Due | None:
    """A due date on its own: ISO ("2026-10-20", "2026-10-20T09:00") or words ("fri 3pm")."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return parse_quick(raw, now).due
    tz = now.tzinfo or UTC
    if len(raw) <= 10:  # a date with no time
        return Due(day_end(parsed.date(), tz), all_day=True)
    local = parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    return Due(local.astimezone(UTC), all_day=False)


def next_due(due_at: datetime, repeat: str, tz: tzinfo, now: datetime) -> datetime:
    """The next occurrence after `now` of a repeating task, keeping its local time of day."""
    local = due_at.astimezone(tz)
    d, clock = local.date(), local.time()
    for _ in range(4000):  # ~11 years of daily steps: a safe upper bound
        if repeat == "daily":
            d += timedelta(days=1)
        elif repeat == "weekdays":
            d += timedelta(days={4: 3, 5: 2}.get(d.weekday(), 1))
        elif repeat == "weekly":
            d += timedelta(days=7)
        elif repeat == "monthly":
            d = add_months(d, 1)
        else:
            raise ValueError(f"Unknown repeat '{repeat}'")
        candidate = datetime.combine(d, clock, tz).astimezone(UTC)
        if candidate > now:
            return candidate
    raise ValueError("Could not find the next occurrence")
