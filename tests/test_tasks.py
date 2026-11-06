from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from neurovex.core.db import utcnow
from neurovex.core.skills import SkillRegistry
from neurovex.modules.tasks.dates import Due, day_end, next_due, parse_due, parse_quick
from neurovex.modules.tasks.models import Task
from neurovex.modules.tasks.module import TasksModule
from neurovex.modules.tasks.planner import bucket, due_label, suggest
from neurovex.modules.tasks.skills import tasks_skills
from neurovex.modules.tasks.store import TaskError, TaskStore
from tests.test_api import HEADERS, client  # noqa: F401  (fixture)
from tests.test_chat import chat_for, news, say, use  # noqa: F401  (fixture)

TZ = ZoneInfo("Asia/Dhaka")
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=TZ)  # a Wednesday morning


# --- quick-add and dates ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "title", "when"),
    [
        ("Call bank tomorrow 10am", "Call bank", "2026-10-15 10:00"),
        ("Submit report fri", "Submit report", "2026-10-16"),
        ("Buy milk at 5", "Buy milk", "2026-10-14 17:00"),
        ("dentist 2026-10-20 at 9:30", "dentist", "2026-10-20 09:30"),
        ("stretch in 2 hours", "stretch", "2026-10-14 12:00"),
        ("Watch movie tonight", "Watch movie", "2026-10-14 20:00"),
        ("Watch movie tonight at 9pm", "Watch movie", "2026-10-14 21:00"),
        ("Call Ayesha tomorrow morning", "Call Ayesha", "2026-10-15 09:00"),
        ("Email Oct 3", "Email", "2027-10-03"),  # already passed this year
        ("Plan trip next week", "Plan trip", "2026-10-19"),
        ("Lunch with Sam at noon on 20 oct", "Lunch with Sam", "2026-10-20 12:00"),
        ("Call mom on sun", "Call mom", "2026-10-18"),
        ("Fix bug #123 by monday", "Fix bug #123", "2026-10-19"),
    ],
)
def test_quick_add_reads_dates(text, title, when):
    p = parse_quick(text, NOW)
    all_day = len(when) == 10
    assert p.title == title
    assert p.due is not None and p.due.all_day is all_day
    assert p.due.at.astimezone(TZ).strftime("%Y-%m-%d" if all_day else "%Y-%m-%d %H:%M") == when


@pytest.mark.parametrize(
    "text",
    [
        "Read chapter 5",
        "Prepare Friday's slides",
        "Study for SAT",
        "Go out in the sun",
        "Morning run",
        "Today's report",
        "I may 2 things",
    ],
)
def test_quick_add_leaves_ordinary_words_alone(text):
    p = parse_quick(text, NOW)
    assert (p.title, p.due, p.tags) == (text, None, [])


def test_quick_add_priority_tags_estimate_and_repeats():
    p = parse_quick("Call bank tomorrow 10am !high #errands #Money ~1h30m", NOW)
    assert (p.title, p.priority, p.tags, p.estimate_min) == (
        "Call bank", 3, ["errands", "money"], 90,
    )  # fmt: skip
    assert parse_quick("Fix prod !!", NOW).priority == 4
    assert parse_quick("Tidy desk !low", NOW).priority == 1
    rent = parse_quick("Pay rent every month", NOW)
    assert rent.repeat == "monthly" and rent.due.all_day
    assert rent.due.at.astimezone(TZ).date() == NOW.date()
    standup = parse_quick("standup every weekday 9am", NOW)  # today's 9:00 has passed
    assert standup.repeat == "weekdays"
    assert standup.due.at.astimezone(TZ).strftime("%a %H:%M") == "Thu 09:00"
    plants = parse_quick("Water plants every sunday", NOW)
    assert plants.repeat == "weekly" and plants.due.at.astimezone(TZ).strftime("%a") == "Sun"


def test_parse_due_iso_and_words():
    assert parse_due("2026-10-20", NOW).all_day
    timed = parse_due("2026-10-20T09:00", NOW)
    assert not timed.all_day and timed.at.astimezone(TZ).hour == 9
    assert parse_due("fri 3pm", NOW).at.astimezone(TZ).strftime("%a %H:%M") == "Fri 15:00"
    assert parse_due("asap", NOW) is None


def test_next_due_keeps_local_time_and_skips_missed_days():
    def nxt(start: datetime, repeat: str, now: datetime = NOW) -> datetime:
        return next_due(start.astimezone(UTC), repeat, TZ, now).astimezone(TZ)

    fri_9 = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
    assert nxt(fri_9, "weekdays", fri_9).strftime("%a %d %H:%M") == "Mon 19 09:00"
    assert nxt(fri_9, "daily", fri_9).day == 17
    assert nxt(fri_9, "weekly", fri_9).day == 23
    jan31 = datetime(2027, 1, 31, 23, 59, 59, tzinfo=TZ)
    assert nxt(jan31, "monthly", jan31).date().isoformat() == "2027-02-28"
    old = datetime(2026, 10, 10, 9, 0, tzinfo=TZ)  # four missed days
    assert nxt(old, "daily").strftime("%d %H:%M") == "15 09:00"


# --- planner (advisor, labels) ----------------------------------------------------------------


def _task(id_: int, title: str, **kw) -> Task:
    age = kw.pop("age_days", 1)
    defaults = {"status": "open", "priority": 2, "all_day": True, "deferrals": 0, "tags": []}
    return Task(id=id_, title=title, created_at=NOW - timedelta(days=age), **{**defaults, **kw})


def test_advisor_ranks_by_urgency_with_reasons():
    today = NOW.date()
    tasks = [
        _task(1, "Someday idea"),
        _task(2, "Overdue report", due_at=day_end(today - timedelta(days=1), TZ)),
        _task(3, "Urgent fix", priority=4),
        _task(4, "Due next week", due_at=day_end(today + timedelta(days=6), TZ)),
        _task(5, "Long essay", due_at=day_end(today, TZ), estimate_min=120),
        _task(6, "Old chore", age_days=20, deferrals=3),
    ]
    ranked = suggest(tasks, NOW, TZ, limit=6)
    assert [s.task.id for s in ranked] == [2, 5, 3, 6, 4, 1]
    assert ranked[0].reasons == ["overdue by 1 day"]
    chore = next(s for s in ranked if s.task.id == 6)
    assert "waiting 20 days" in chore.reasons and any("pushed back 3×" in r for r in chore.reasons)
    # With only 30 minutes, the two-hour essay drops below shorter work.
    short = suggest(tasks, NOW, TZ, minutes=30, limit=6)
    assert [s.task.id for s in short][:3] == [2, 3, 6]
    assert "needs ~120 min, more than you have" in next(s for s in short if s.task.id == 5).reasons


def test_labels_and_buckets():
    today = NOW.date()
    timed = _task(1, "a", due_at=datetime(2026, 10, 15, 10, tzinfo=TZ), all_day=False)
    assert due_label(timed, NOW, TZ) == "tomorrow 10:00" and bucket(timed, NOW, TZ) == "upcoming"
    assert due_label(_task(2, "b", due_at=day_end(today, TZ)), NOW, TZ) == "today"
    sat = _task(3, "c", due_at=day_end(today + timedelta(days=3), TZ))
    assert due_label(sat, NOW, TZ) == "Sat"
    far = _task(4, "d", due_at=day_end(today + timedelta(days=12), TZ))
    assert due_label(far, NOW, TZ) == "Mon 26 Oct" and bucket(far, NOW, TZ) == "later"
    late = _task(5, "e", due_at=datetime(2026, 10, 14, 9, tzinfo=TZ), all_day=False)
    assert bucket(late, NOW, TZ) == "overdue" and bucket(_task(6, "f"), NOW, TZ) == "someday"


# --- store ----------------------------------------------------------------------------------


async def test_store_views_repeats_and_undo(sessions):
    store = TaskStore(sessions, "Asia/Dhaka")
    events: list[tuple[str, str]] = []
    store.on_change = lambda action, t: events.append((action, t.title))
    today = store.local_now().date()
    water = await store.quick_add("Water plants every day")
    late = await store.quick_add("Old form 2020-01-01")
    idea = await store.quick_add("Read SICP")
    sprint = await store.add(
        "Plan sprint", due=Due(day_end(today + timedelta(days=3), store.tz), True)
    )

    assert [t.id for t in await store.list("today")] == [late.id, water.id]
    assert [t.id for t in await store.list("overdue")] == [late.id]
    assert [t.id for t in await store.list("upcoming")] == [sprint.id]
    assert [t.id for t in await store.list("someday")] == [idea.id]
    assert await store.counts() == {"overdue": 1, "today": 1, "open": 4}

    done, nxt = await store.complete(water.id)
    assert done.status == "done" and nxt is not None and nxt.repeat == "daily"
    assert nxt.due_at.astimezone(store.tz).date() == today + timedelta(days=1)
    assert [t.title for t in await store.list("done")] == ["Water plants"]
    assert ("completed", "Water plants") in events and ("added", "Water plants") in events
    # Undo removes the untouched next occurrence instead of leaving a duplicate.
    await store.reopen(water.id)
    assert [t.id for t in await store.list("open", "water")] == [water.id]

    await store.cancel(idea.id)
    assert idea.id not in [t.id for t in await store.list("open")]
    assert await store.delete(sprint.id) and await store.get(sprint.id) is None


async def test_store_reminders_deferrals_and_lookup(sessions):
    store = TaskStore(sessions, "Asia/Dhaka")
    soon = utcnow() - timedelta(minutes=1)
    bank = await store.add("Call bank", due=Due(soon, all_day=False))
    assert bank.remind_at == soon  # timed tasks remind when due
    assert [t.id for t in await store.due_reminders(utcnow())] == [bank.id]
    await store.mark_reminded([bank.id], utcnow())
    assert await store.due_reminders(utcnow()) == []

    moved = await store.postpone(bank.id)
    assert moved.deferrals == 1 and moved.due_at > soon
    assert moved.remind_at == moved.due_at and moved.reminded_at is None  # reminder follows
    snoozed = await store.snooze(bank.id, 10)
    assert snoozed.remind_at > utcnow() and snoozed.remind_at != snoozed.due_at

    await store.add("Call bank about the loan")
    assert (await store.resolve("call bank")).id == bank.id  # exact title wins
    assert (await store.resolve(f"#{bank.id}")).id == bank.id
    with pytest.raises(TaskError, match="Several open tasks match 'bank'"):
        await store.resolve("bank")
    with pytest.raises(TaskError, match="No open task"):
        await store.resolve("dentist")
    with pytest.raises(TaskError):
        await store.add("   ")


async def test_reminders_wait_for_a_listener_then_fire_once(settings, sessions):
    module = TasksModule(settings, sessions)  # loop not started: checks run by hand
    await module.store.add("Stand up", due=Due(utcnow() - timedelta(minutes=3), all_day=False))
    assert await module.check_reminders() == 0  # no page open: kept for later
    with module.live.subscribe() as q:
        assert await module.check_reminders() == 1
        event, data = q.get_nowait()
        assert event == "reminder" and data["title"] == "Stand up" and data["late_min"] >= 3
        assert await module.check_reminders() == 0  # once only
    module.live.close()


# --- chat skills ------------------------------------------------------------------------------


async def test_task_skills(sessions):
    reg = SkillRegistry()
    reg.register_all(tasks_skills(TaskStore(sessions, "Asia/Dhaka")))
    added = await reg.execute(
        "tasks.add", {"title": "Renew passport", "due": "2030-01-04 15:00", "priority": "high"}
    )
    assert added.ok and added.summary == "Added 'Renew passport' · due Fri 4 Jan 2030 15:00"
    assert added.display["kind"] == "tasks" and added.data[0]["priority"] == "high"
    bad = await reg.execute("tasks.add", {"title": "Something", "due": "whenever"})
    assert not bad.ok and "Couldn't read the date 'whenever'" in bad.error

    await reg.execute("tasks.add", {"title": "Water plants", "repeat": "daily"})
    listed = await reg.execute("tasks.list", {"view": "open"})
    assert [t["title"] for t in listed.data["tasks"]] == ["Water plants", "Renew passport"]
    assert listed.data["totals"]["open"] == 2

    moved = await reg.execute("tasks.update", {"task": "passport", "due": "none"})
    assert moved.ok and "due" not in moved.data[0]
    assert not (await reg.execute("tasks.update", {"task": "passport"})).ok
    rolled = await reg.execute("tasks.complete", {"task": "water plants"})
    assert rolled.ok and "next one · due tomorrow" in rolled.summary
    nxt = await reg.execute("tasks.next", {})
    assert nxt.ok and nxt.data["suggestions"][0]["why"]
    assert (await reg.execute("tasks.cancel", {"task": "passport"})).summary == (
        "Dropped 'Renew passport'"
    )
    sig = reg.describe()
    assert "priority?: low|normal|high|urgent" in sig  # optional enums keep their options
    assert "repeat?: none|daily|weekdays|weekly|monthly" in sig


def act(tool: str, message: str, then: list | None = None, **args) -> str:
    """A use_tool step that carries its final reply (protocol: changes, the last tools)."""
    step = {"action": "use_tool", "tool": tool, "arguments_json": json.dumps(args)}
    extra = [{"tool": t, "arguments_json": json.dumps(a)} for t, a in then or []]
    return json.dumps({**step, "message": message, "then": extra})


async def test_chat_adds_a_task(settings, sessions, news):  # noqa: F811
    engine = chat_for(settings, news, sessions, [
        use("tasks.add", title="Call the bank", due="tomorrow 10am"),
        say("Added: call the bank tomorrow at 10:00."),
    ])  # fmt: skip
    turn = await engine.ask("Remind me to call the bank tomorrow at 10")
    assert turn.reply.startswith("Added") and turn.cards[0]["kind"] == "tasks"
    tasks = await TaskStore(sessions).list("open")
    assert [(t.title, t.source, t.all_day) for t in tasks] == [("Call the bank", "chat", False)]


async def test_an_action_with_its_reply_takes_one_model_call(settings, sessions, news):  # noqa: F811
    engine = chat_for(settings, news, sessions, [
        act("tasks.add", "Added: stretch in an hour.", title="Stretch", due="in 1 hour"),
    ])  # fmt: skip
    provider = engine.router.states["groq"].provider
    turn = await engine.ask("Remind me to stretch in an hour")
    assert turn.reply == "Added: stretch in an hour." and provider.calls == 1
    assert [s["tool"] for s in turn.steps] == ["tasks.add"] and turn.cards[0]["kind"] == "tasks"


async def test_early_replies_only_for_successful_changes(settings, sessions, news):  # noqa: F811
    engine = chat_for(settings, news, sessions, [
        act("tasks.list", "You're all clear.", view="today"),  # a read: must see results first
        say("Nothing is due today."),
        act("tasks.complete", "Done!", task="dentist"),  # fails: the model hears why
        say("I couldn't find a task called dentist."),
    ])  # fmt: skip
    assert (await engine.ask("What's due today?")).reply == "Nothing is due today."
    turn = await engine.ask("I went to the dentist")
    assert turn.reply == "I couldn't find a task called dentist."
    assert turn.steps[0]["ok"] is False


async def test_several_things_in_one_step(settings, sessions, news):  # noqa: F811
    """ "Add X and Y" runs both calls and replies in a single model call."""
    engine = chat_for(settings, news, sessions, [
        act("tasks.add", "Added both.", then=[("tasks.add", {"title": "Water plants",
            "repeat": "daily"})], title="Renew passport", due="end of month"),
    ])  # fmt: skip
    provider = engine.router.states["groq"].provider
    turn = await engine.ask("I need to renew my passport this month and water the plants daily")
    assert turn.reply == "Added both." and provider.calls == 1
    assert [s["tool"] for s in turn.steps] == ["tasks.add", "tasks.add"]
    titles = {t.title: t.repeat for t in await TaskStore(sessions).list("open")}
    assert titles == {"Renew passport": None, "Water plants": "daily"}


async def test_focused_requests_get_a_smaller_prompt(settings, sessions, news):  # noqa: F811
    engine = chat_for(settings, news, sessions, [say("Sure.")] * 3)
    reg, provider = engine.skills, engine.router.states["groq"].provider
    assert reg.groups_for("Remind me to call the bank tomorrow") == {"tasks", "memory", "system"}
    assert reg.groups_for("Hi there") is None  # nothing matched: every tool, in full
    assert reg.groups_for("And the second one?", sticky={"news"}) == {"news", "memory", "system"}
    assert reg.groups_for("Hi", sticky={"memory"}) is None  # only hinted groups stick
    assert reg.groups_for("Move it to friday", sticky={"news"}) >= {"tasks", "news"}

    await engine.ask("Remind me to call the bank tomorrow")
    focused = provider.requests[-1].system
    assert "tasks.add(title: str" in focused and "news.search(" not in focused
    assert "Also available (arguments are checked): news.search, news.top" in focused
    await engine.ask("Hi there")
    assert "news.search(query?: str" in provider.requests[-1].system
    assert len(focused) < len(provider.requests[-1].system) - 1000


# --- API --------------------------------------------------------------------------------------


async def test_tasks_api(client):  # noqa: F811
    r = await client.post("/api/tasks", json={"text": "Call bank tomorrow 10am !high #errands"})
    assert r.status_code == 403  # writes need the client header
    r = await client.post(
        "/api/tasks", json={"text": "Call bank tomorrow 10am !high #errands"}, headers=HEADERS
    )
    assert r.status_code == 201
    bank = r.json()
    assert (bank["title"], bank["priority_name"], bank["tags"]) == (
        "Call bank",
        "high",
        ["errands"],
    )
    assert bank["due_label"] == "tomorrow 10:00" and bank["bucket"] == "upcoming"
    assert bank["remind_at"] == bank["due_at"]

    r = await client.post(
        "/api/tasks", json={"title": "Old form", "due": "2020-01-01"}, headers=HEADERS
    )
    old = r.json()
    assert old["bucket"] == "overdue"
    bad = await client.post(
        "/api/tasks", json={"title": "x", "due": "someday maybe"}, headers=HEADERS
    )
    assert bad.status_code == 422

    agenda = (await client.get("/api/tasks/agenda")).json()
    assert [t["id"] for t in agenda["groups"]["overdue"]] == [old["id"]]
    assert agenda["counts"] == {"overdue": 1, "today": 0, "open": 2}
    picks = (await client.get("/api/tasks/next")).json()
    assert picks[0]["task"]["id"] == old["id"] and picks[0]["reasons"]
    preview = (
        await client.get("/api/tasks/parse", params={"text": "Gym every weekday 7am"})
    ).json()
    assert preview["title"] == "Gym" and preview["repeat"] == "weekdays"

    r = await client.patch(
        f"/api/tasks/{bank['id']}", json={"due": None, "priority": 4}, headers=HEADERS
    )
    assert r.json()["due_at"] is None and r.json()["priority_name"] == "urgent"
    # Editing with quick-add text: what it mentions changes, the rest stays.
    r = await client.patch(
        f"/api/tasks/{bank['id']}", json={"text": "Call the bank fri 3pm #money"}, headers=HEADERS
    )
    edited = r.json()
    assert (edited["title"], edited["tags"], edited["priority_name"]) == (
        "Call the bank", ["money"], "urgent",
    )  # fmt: skip
    assert edited["due_at"] and not edited["all_day"] and edited["due_label"].endswith("15:00")
    assert (await client.get("/api/tasks/counts")).json()["open"] == 2
    assert (await client.get("/health")).json()["modules"]["tasks"]["overdue"] == 1
    r = await client.post(f"/api/tasks/{old['id']}/postpone", json={"days": 2}, headers=HEADERS)
    assert r.json()["deferrals"] == 1 and r.json()["bucket"] == "upcoming"
    r = await client.post(f"/api/tasks/{old['id']}/complete", headers=HEADERS)
    assert r.json()["task"]["status"] == "done" and r.json()["next"] is None
    assert [t["id"] for t in (await client.get("/api/tasks/agenda")).json()["done_today"]] == [
        old["id"]
    ]
    r = await client.post(f"/api/tasks/{old['id']}/reopen", headers=HEADERS)
    assert r.json()["status"] == "open"
    assert (await client.delete(f"/api/tasks/{old['id']}", headers=HEADERS)).status_code == 204
    assert (await client.get(f"/api/tasks/{old['id']}")).status_code == 404
    assert (await client.get("/api/tasks", params={"view": "nope"})).status_code == 422
