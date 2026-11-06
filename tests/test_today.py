from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from neurovex.core.config import Profile, Settings
from neurovex.memory.models import Memory
from neurovex.memory.store import MemoryStore
from neurovex.modules.tasks.dates import Due, day_end
from neurovex.modules.tasks.store import TaskStore
from neurovex.today.brief import TodayBrief, date_label, greeting, name_from, summary_line
from neurovex.today.module import TodayModule, in_window
from tests.test_api import HEADERS, client  # noqa: F401  (fixture)
from tests.test_chat import chat_for, news, say, use  # noqa: F401  (fixture)

TZ = ZoneInfo("Asia/Dhaka")
MORNING = datetime(2026, 11, 4, 8, 30, tzinfo=TZ)  # a Wednesday


# --- pure pieces ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overdue", "due", "done", "title", "new", "line"),
    [
        (0, 0, 0, None, 0, "Nothing due today."),
        (0, 0, 0, "Read SICP", 0, "Nothing due today. Next up: Read SICP."),
        (2, 3, 0, "Pay rent", 0, "2 overdue and 3 still due today. Start with Pay rent."),
        (1, 0, 0, "Pay rent", 1, "1 overdue. Start with Pay rent. 1 new story since your last visit."),
        (0, 1, 0, "Call", 4, "1 still due today. Start with Call. 4 new stories since your last visit."),
        (0, 0, 3, None, 0, "You've finished 3 today."),
    ],
)  # fmt: skip
def test_summary_line(overdue, due, done, title, new, line):
    assert summary_line(overdue, due, done, title, new) == line


def test_greeting_date_and_name():
    assert greeting(MORNING) == "Good morning"
    assert greeting(MORNING.replace(hour=13), "Sam") == "Good afternoon, Sam"
    assert greeting(MORNING.replace(hour=20)) == greeting(MORNING.replace(hour=2)) == "Good evening"
    assert date_label(MORNING) == "Wednesday 4 November"
    memories = [Memory(text="Works as a nurse"), Memory(text="Name is Sam")]
    assert name_from(memories) == "Sam"
    assert name_from([Memory(text="Prefers to be called Mahi")]) == "Mahi"
    assert name_from([Memory(text="Works as a nurse")]) is None


def test_announce_window():
    assert in_window(MORNING, "08:00")
    assert not in_window(MORNING.replace(hour=7), "08:00")  # too early
    assert in_window(MORNING.replace(hour=13, minute=59), "08:00")
    assert not in_window(MORNING.replace(hour=14), "08:00")  # too late to say good morning
    assert not in_window(MORNING, "")  # switched off


def test_brief_time_setting_is_validated():
    assert (
        Settings(_env_file=None, profile=Profile.TEST, today_brief_time=" 07:30 ").today_brief_time
        == "07:30"
    )
    assert (
        Settings(_env_file=None, profile=Profile.TEST, today_brief_time="").today_brief_time == ""
    )
    with pytest.raises(ValueError, match="08:00"):
        Settings(_env_file=None, profile=Profile.TEST, today_brief_time="25:99")


# --- the brief --------------------------------------------------------------------------------


async def _seed(sessions) -> tuple[TaskStore, MemoryStore]:
    tasks, memory = TaskStore(sessions, "Asia/Dhaka"), MemoryStore(sessions)
    today = tasks.local_now().date()
    await tasks.quick_add("Old form 2020-01-01")
    await tasks.add("Pay rent", due=Due(tasks.end_of_today(), all_day=True))
    await tasks.add("Call bank", due=Due(day_end(today + timedelta(days=1), tasks.tz), True))
    await tasks.quick_add("Read SICP")
    done = await tasks.add("Water plants", due=Due(tasks.end_of_today(), all_day=True))
    await tasks.complete(done.id)
    await memory.add("Name is Mahi", "fact", 2)
    await memory.add("Wants to ship Neurovex v1 by March", "goal", 2)
    return tasks, memory


async def test_brief_brings_the_day_together(sessions, news):  # noqa: F811
    tasks, memory = await _seed(sessions)
    t = await TodayBrief(sessions, tasks, memory).build()
    assert t.greeting.endswith(", Mahi") and t.part_of_day in ("morning", "afternoon", "evening")
    assert [x.title for x in t.overdue] == ["Old form"]
    assert [x.title for x in t.today] == ["Pay rent"]
    assert [x.title for x in t.tomorrow] == ["Call bank"]
    assert [x.title for x in t.done_today] == ["Water plants"]
    assert t.next is not None and t.next.task.title == "Old form" and t.next.reasons
    assert t.line.startswith("You've finished 1 today. 1 overdue and 1 still due today.")
    assert t.counts == {"overdue": 1, "today": 1, "open": 4}
    assert t.goals == ["Wants to ship Neurovex v1 by March"]
    assert t.stories_window_hours == 24
    assert {a.title for a in t.stories} >= {"Critical Windows flaw patched", "Rust 2.0 released"}
    assert t.briefing is not None and t.briefing.headline == "Your news, briefly"


async def test_brief_works_on_an_empty_install(sessions):
    t = await TodayBrief(sessions, TaskStore(sessions), MemoryStore(sessions)).build()
    assert t.line == "Nothing due today." and t.next is None and t.stories == []
    assert t.greeting.startswith("Good ") and "," not in t.greeting


async def test_brief_makes_no_ai_calls(settings, sessions, news):  # noqa: F811
    engine = chat_for(settings, news, sessions, [])
    tasks, memory = await _seed(sessions)
    await TodayBrief(sessions, tasks, memory).build()
    assert engine.router.states["groq"].provider.calls == 0


# --- the morning announcement -----------------------------------------------------------------


async def test_morning_announcement_fires_once_and_needs_a_listener(settings, sessions):
    tasks = TaskStore(sessions, "Asia/Dhaka")
    module = TodayModule(
        settings.model_copy(update={"today_brief_time": "08:00"}),
        TodayBrief(sessions, tasks, MemoryStore(sessions)),
    )
    assert not await module.check_morning(MORNING)  # no page open: stays unannounced
    with module.live.subscribe() as q:
        assert not await module.check_morning(MORNING.replace(hour=7))  # too early
        assert await module.check_morning(MORNING)
        event, data = q.get_nowait()
        assert event == "today" and data["line"] == "Nothing due today."
        assert not await module.check_morning(MORNING + timedelta(minutes=5))  # once a day
        assert await module.check_morning(MORNING + timedelta(days=1))  # and again tomorrow
    module.live.close()


# --- chat -------------------------------------------------------------------------------------


async def test_chat_can_brief_the_user(settings, sessions, news):  # noqa: F811
    await _seed(sessions)
    engine = chat_for(settings, news, sessions, [use("today.brief"), say("Start with Old form.")])
    assert engine.skills.groups_for("Brief me on my day") >= {"today"}
    turn = await engine.ask("Brief me on my day")
    assert turn.reply == "Start with Old form." and turn.steps[0]["ok"]
    assert turn.cards[0]["kind"] == "tasks"
    assert {i["title"] for i in turn.cards[0]["items"]} == {"Old form", "Pay rent"}
    fenced = engine.router.states["groq"].provider.requests[-1].user
    assert '"do_first"' in fenced and "Critical Windows flaw patched" in fenced


# --- API --------------------------------------------------------------------------------------


async def test_today_api(client):  # noqa: F811
    await client.post(
        "/api/tasks", json={"title": "Old form", "due": "2020-01-01"}, headers=HEADERS
    )
    r = await client.get("/api/today")
    assert r.status_code == 200
    t = r.json()
    assert t["next"]["task"]["title"] == "Old form" and t["overdue"][0]["bucket"] == "overdue"
    assert t["line"].startswith("1 overdue.") and t["date_label"]
    assert t["counts"]["overdue"] == 1 and t["stories"] == []
