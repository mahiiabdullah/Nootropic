"""`nvx`: the Neurovex command line."""

from __future__ import annotations

import asyncio
import getpass
import os
import platform
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from neurovex import __version__
from neurovex.core.config import get_settings, set_secret
from neurovex.core.db import create_engine, create_sessionmaker, init_db, utcnow
from neurovex.core.logging import configure_logging

app = typer.Typer(help="Neurovex: your personal AI operating system.", no_args_is_help=True)
news_app = typer.Typer(help="Tech news: refresh, browse, digest.", no_args_is_help=True)
sources_app = typer.Typer(help="Manage news feeds.", no_args_is_help=True)
secrets_app = typer.Typer(help="Store API keys in the OS keychain.", no_args_is_help=True)
llm_app = typer.Typer(help="AI providers: status, tests, models, setup.", no_args_is_help=True)
memory_app = typer.Typer(help="What Neurovex remembers about you.", no_args_is_help=True)
tasks_app = typer.Typer(help="Your tasks: add, list, done, what's next.")
app.add_typer(news_app, name="news")
news_app.add_typer(sources_app, name="sources")
app.add_typer(secrets_app, name="secrets")
app.add_typer(llm_app, name="llm")
app.add_typer(memory_app, name="memory")
app.add_typer(tasks_app, name="tasks")

# Windows consoles default to a legacy code page; article titles need UTF-8.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

console = Console()


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    secs = int((utcnow() - dt).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def _run_with_news[T](fn: Callable[..., Awaitable[T]]) -> T:
    """Open the DB and a NewsModule (without the refresh loop) for a CLI command."""
    from neurovex.modules.news.module import NewsModule

    settings = get_settings().model_copy(update={"news_auto_refresh": False})
    settings.ensure_dirs()
    configure_logging(settings.model_copy(update={"log_level": "WARNING"}))

    async def main() -> T:
        from neurovex.llm.router import LLMRouter
        from neurovex.llm.usage import db_recorder
        from neurovex.modules.news.summarizer import router_summarizer_factory

        engine = create_engine(settings.db_url)
        await init_db(engine)
        sessions = create_sessionmaker(engine)
        router = LLMRouter.from_settings(settings, db_recorder(sessions))
        news = NewsModule(settings, sessions, router_summarizer_factory(router))
        news.llm = router  # type: ignore[attr-defined]
        await news.start()
        try:
            return await fn(news)
        finally:
            await news.stop()
            await router.aclose()
            await engine.dispose()

    return asyncio.run(main())


# --- core ------------------------------------------------------------------------


@app.command()
def serve(
    port: Annotated[int | None, typer.Option(help="Port (default from settings)")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes (dev)")] = False,
) -> None:
    """Run the Neurovex server and web UI. Ctrl+C stops it cleanly."""
    from neurovex import server

    settings = get_settings()
    settings.check_bind_host()
    if port:  # make the app (and reload workers) see the same port
        os.environ["NEUROVEX_PORT"] = str(port)
        get_settings.cache_clear()
    port = port or settings.port
    console.print(f"[bold #7BA7BC]Neurovex[/] {__version__} → http://{settings.host}:{port}")
    server.run(settings.host, port, reload=reload)


@app.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


@app.command()
def doctor() -> None:
    """Check that everything Neurovex needs is in place."""
    import httpx

    settings = get_settings()
    table = Table(title="Neurovex doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    def row(name: str, ok: bool | None, detail: str) -> None:
        mark = {True: "[green]OK[/]", False: "[red]FAIL[/]", None: "[yellow]WARN[/]"}[ok]
        table.add_row(name, mark, detail)

    py_ok = sys.version_info >= (3, 12)
    row("Python", py_ok, f"{platform.python_version()} ({sys.executable})")
    try:
        settings.ensure_dirs()
        probe = settings.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        row("Data dir", True, str(settings.data_dir))
    except OSError as e:
        row("Data dir", False, f"{settings.data_dir}: {e}")

    from neurovex.llm.catalog import PROVIDERS

    order = settings.llm_provider_order()
    ready = []
    for pid in order:
        spec = PROVIDERS[pid]
        if spec.kind == "ollama":
            try:
                httpx.get(f"{settings.ollama_url}/api/tags", timeout=3)
                row(f"AI: {spec.name}", True, "local, reachable (dev only)")
                ready.append(pid)
            except httpx.HTTPError:
                row(f"AI: {spec.name}", None, f"not reachable at {settings.ollama_url}")
            continue
        has_key = bool(settings.secret(spec.key_env, *spec.alt_key_envs))
        has_account = not spec.account_env or bool(settings.secret(spec.account_env))
        model = settings.model_for(pid) or spec.default_model
        if has_key and has_account:
            ready.append(pid)
            row(f"AI: {spec.name}", True, f"key found · model {model}")
        else:
            missing = spec.key_env if not has_key else spec.account_env
            row(f"AI: {spec.name}", None, f"set {missing}  ({spec.signup_url})")
    row(
        "AI providers",
        bool(ready),
        f"{len(ready)}/{len(order)} ready · strategy {settings.llm_strategy}"
        + ("" if ready else " · run `nvx llm setup`"),
    )

    try:
        import psutil  # optional

        vm = psutil.virtual_memory()
        row("RAM", vm.available > 3 * 2**30 or None,
            f"{vm.available / 2**30:.1f} GB free of {vm.total / 2**30:.1f} GB")  # fmt: skip
    except ImportError:
        pass
    row("News summaries", True, f"news_llm_provider = {settings.news_llm_provider}")
    console.print(table)


# --- secrets --------------------------------------------------------------------


@secrets_app.command("set")
def secrets_set(name: Annotated[str, typer.Argument(help="e.g. ANTHROPIC_API_KEY")]) -> None:
    """Save a secret to Windows Credential Manager (input is hidden)."""
    value = getpass.getpass(f"{name}: ").strip()
    if not value:
        raise typer.BadParameter("Empty value, nothing saved.")
    set_secret(name, value)
    console.print(f"[green]Saved[/] {name} to the OS keychain (service 'neurovex').")


@secrets_app.command("status")
def secrets_status() -> None:
    """Show which known secrets are set (never prints values)."""
    from neurovex.llm.catalog import PROVIDERS

    settings = get_settings()
    specs = [PROVIDERS[pid] for pid in settings.llm_provider_order()]
    names = [n for spec in specs for n in (spec.key_env, spec.account_env) if n]
    for name in names:
        found = settings.secret(name)
        console.print(f"{name:<24} {'[green]set[/]' if found else '[dim]not set[/]'}")


# --- chat -----------------------------------------------------------------------


def _print_turn(turn) -> None:  # type: ignore[no-untyped-def]
    from rich.markdown import Markdown

    for step in turn.steps:
        mark = "›" if step["ok"] else "[red]✕[/]"
        console.print(f"[dim]{mark} {step['tool']} · {step['summary']}[/]")
    console.print(Markdown(turn.reply), style="red" if turn.error else None)
    for m in turn.memory:
        console.print(f"[#7BA7BC]✦ {m['op']}[/] [dim]({m['kind']})[/] {m['text']}")
    for card in turn.cards:
        for item in card.get("items", [])[:6]:
            imp = item.get("importance") or "-"
            console.print(f"  [dim]{imp:>2}[/] [link={item['url']}]{item['title']}[/link] "
                          f"[dim]({item['source']})[/]")  # fmt: skip
    if turn.model:
        console.print(f"[dim]{turn.model}[/]")


@app.command()
def chat(
    message: Annotated[str | None, typer.Argument(help="Ask one question and exit")] = None,
) -> None:
    """Talk to Neurovex: ask about your news, follow topics, get your briefing."""
    from neurovex.main import build_chat

    async def go(news):  # type: ignore[no-untyped-def]
        engine = build_chat(news.llm, news, news.sessions)
        if message:
            with console.status("Thinking..."):
                _print_turn(await engine.ask(message))
            return
        console.print(
            "[bold #7BA7BC]Neurovex[/] [dim]· ask anything about your news · /new starts over"
            " · /exit quits[/]"
        )
        conversation: int | None = None
        while True:
            try:
                text = (await asyncio.to_thread(console.input, "[bold]you ›[/] ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/new":
                conversation = None
                console.print("[dim]New conversation.[/]")
                continue
            with console.status("Thinking..."):
                turn = await engine.ask(text, conversation)
            conversation = turn.conversation_id
            _print_turn(turn)
            console.print()

    _run_with_news(go)


# --- memory ---------------------------------------------------------------------


def _with_memory[T](fn: Callable[..., Awaitable[T]]) -> T:
    from neurovex.memory.store import MemoryStore

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.model_copy(update={"log_level": "ERROR"}))

    async def main() -> T:
        engine = create_engine(settings.db_url)
        await init_db(engine)
        try:
            return await fn(MemoryStore(create_sessionmaker(engine)))
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _memory_table(items) -> Table:  # type: ignore[no-untyped-def]
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Kind", style="#7BA7BC")
    table.add_column("Memory", ratio=1)
    table.add_column("Imp", justify="center")
    table.add_column("Used", style="dim")
    for m in items:
        pin = "📌 " if m.pinned else ""
        table.add_row(str(m.id), m.kind, pin + m.text, str(m.importance), str(m.use_count))
    return table


@memory_app.command("list")
def memory_list(kind: Annotated[str | None, typer.Option("--kind", "-k")] = None) -> None:
    """Everything Neurovex remembers (optionally one kind)."""
    items = _with_memory(lambda store: store.list(kind=kind))
    if not items:
        console.print("[dim]Nothing remembered yet. Tell Neurovex about yourself in `nvx chat`.[/]")
        return
    console.print(_memory_table(items))


@memory_app.command("search")
def memory_search(query: str) -> None:
    """Search memories and past conversations."""

    async def go(store):  # type: ignore[no-untyped-def]
        return await store.search(query, limit=20), await store.search_conversations(query, 8)

    memories, convs = _with_memory(go)
    if memories:
        console.print(_memory_table(memories))
    for h in convs:
        console.print(f"[dim]{h.at.date()} · {h.title[:50]} · {h.role}[/]  {h.snippet}")
    if not memories and not convs:
        console.print("[dim]No matches.[/]")


@memory_app.command("add")
def memory_add(
    text: str,
    kind: Annotated[
        str,
        typer.Option(
            "--kind", "-k", help="fact | preference | instruction | person | goal | event"
        ),
    ] = "fact",
    importance: Annotated[int, typer.Option("--importance", "-i", min=1, max=3)] = 2,
    pin: Annotated[bool, typer.Option("--pin", help="Always include it")] = False,
) -> None:
    """Teach Neurovex something about you."""
    m, how = _with_memory(
        lambda store: store.add(text, kind, importance, pinned=pin, source="user")
    )
    if m is None:
        console.print(f"[red]Not stored:[/] {how}")
        raise typer.Exit(1)
    console.print(
        f"[green]{'Remembered' if how == 'added' else 'Updated'}[/] #{m.id} ({m.kind}) {m.text}"
    )


@memory_app.command("forget")
def memory_forget(memory_id: int) -> None:
    """Forget one memory by id."""
    m = _with_memory(lambda store: store.forget(memory_id))
    if m is None:
        console.print(f"[red]No memory #{memory_id}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Forgot[/] #{m.id}: {m.text}")


@memory_app.command("clear")
def memory_clear(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the question")] = False,
) -> None:
    """Forget everything Neurovex remembers about you."""
    if not yes and not typer.confirm("Forget everything Neurovex remembers about you?"):
        raise typer.Exit(1)
    n = _with_memory(lambda store: store.clear())
    console.print(f"[green]Forgot {n} memories.[/]")


# --- today ----------------------------------------------------------------------


@app.command()
def today() -> None:
    """Your day at a glance: what's due, what to do first, top stories. Uses no AI."""
    from rich.markup import escape

    from neurovex.memory.store import MemoryStore
    from neurovex.modules.tasks.store import TaskStore
    from neurovex.today.brief import TodayBrief

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.model_copy(update={"log_level": "ERROR"}))

    async def main():  # type: ignore[no-untyped-def]
        engine = create_engine(settings.db_url)
        await init_db(engine)
        sessions = create_sessionmaker(engine)
        try:
            return await TodayBrief(
                sessions, TaskStore(sessions, settings.timezone), MemoryStore(sessions)
            ).build()
        finally:
            await engine.dispose()

    t = asyncio.run(main())
    console.print(f"[bold #7BA7BC]{escape(t.greeting)}[/] [dim]· {t.date_label}[/]")
    console.print(escape(t.line))
    if t.next:
        why = "; ".join(t.next.reasons) or "next in line"
        console.print(
            f"\n[#7BA7BC]Do next ›[/] {escape(t.next.task.title)} [dim]({escape(why)})[/]"
        )
    for label, items in (("OVERDUE", t.overdue), ("TODAY", t.today), ("TOMORROW", t.tomorrow)):
        if items:
            console.print(f"\n[bold]{label}[/] [dim]{len(items)}[/]")
            for x in items[:10]:
                when = f" [dim]{x.due_label}[/]" if x.due_label else ""
                console.print(f"  [dim]#{x.id}[/] {escape(x.title)}{when}")
    if t.briefing:
        console.print(f"\n[bold]BRIEFING[/] {escape(t.briefing.headline)}")
    if t.stories:
        console.print(f"\n[bold]TOP STORIES[/] [dim]last {t.stories_window_hours}h[/]")
        for a in t.stories:
            source = escape(a.source_name)
            console.print(
                f"  [dim]{a.importance or '-':>2}[/] {escape(a.title)} [dim]({source})[/]"
            )
    if t.goals:
        console.print("\n[bold]GOALS[/]")
        for g in t.goals:
            console.print(f"  {escape(g)}")


# --- tasks ----------------------------------------------------------------------

_DUE_STYLE = {"overdue": "#C27474", "today": "#7BA7BC", "upcoming": "default"}
_PRIORITY_MARK = {4: "[#C27474]!!![/] ", 3: "[#C8A96A]![/] ", 1: "[dim]↓[/] "}


def _with_tasks[T](fn: Callable[..., Awaitable[T]]) -> T:
    from neurovex.modules.tasks.store import TaskStore

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.model_copy(update={"log_level": "ERROR"}))

    async def main() -> T:
        engine = create_engine(settings.db_url)
        await init_db(engine)
        try:
            return await fn(TaskStore(create_sessionmaker(engine), settings.timezone))
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _task_line(task, tz) -> str:  # type: ignore[no-untyped-def]
    from rich.markup import escape

    from neurovex.modules.tasks.planner import bucket, due_label

    now = utcnow()
    done = task.status != "open"
    title = f"[strike dim]{escape(task.title)}[/]" if done else escape(task.title)
    meta = []
    if label := due_label(task, now, tz):
        style = "dim" if done else _DUE_STYLE.get(bucket(task, now, tz), "dim")
        meta.append(f"[{style}]{label}[/]")
    if task.repeat:
        meta.append(f"[dim]↻ {task.repeat}[/]")
    if task.estimate_min:
        meta.append(f"[dim]~{task.estimate_min}m[/]")
    meta += [f"[#7BA7BC]#{escape(t)}[/]" for t in task.tags or []]
    mark = "" if done else _PRIORITY_MARK.get(task.priority, "")
    return f"[dim]#{task.id}[/]  {mark}{title}  {'  '.join(meta)}".rstrip()


def _task_cmd[T](fn: Callable[..., Awaitable[T]]) -> T:
    """Run a task command; a TaskError becomes a red message and exit code 1."""
    from neurovex.modules.tasks.store import TaskError

    try:
        return _with_tasks(fn)
    except TaskError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


@tasks_app.callback(invoke_without_command=True)
def tasks_overview(ctx: typer.Context) -> None:
    """Your tasks: overdue, today, the week ahead, and what to do first."""
    if ctx.invoked_subcommand is not None:
        return
    from neurovex.modules.tasks.planner import BUCKETS, bucket, suggest

    async def go(store):  # type: ignore[no-untyped-def]
        return await store.list("open", limit=500), store.tz

    tasks, tz = _task_cmd(go)
    if not tasks:
        console.print('[dim]No open tasks. Add one: nvx tasks add "Call bank tomorrow 10am"[/]')
        return
    now = utcnow()
    for name in BUCKETS:
        group = [t for t in tasks if bucket(t, now, tz) == name]
        if group:
            console.print(f"\n[bold]{name.upper()}[/] [dim]{len(group)}[/]")
            for t in group[:25]:
                console.print(_task_line(t, tz))
    if top := suggest(tasks, now, tz, limit=1):
        why = "; ".join(top[0].reasons) or "open"
        console.print(f"\n[#7BA7BC]Next ›[/] {top[0].task.title} [dim]({why})[/]")


@tasks_app.command("add")
def tasks_add(
    text: Annotated[list[str], typer.Argument(help="e.g. Call bank tomorrow 10am !high #errands")],
) -> None:
    """Add a task in plain words: dates, !priority, #tags, ~estimate, 'every week'."""

    async def go(store):  # type: ignore[no-untyped-def]
        return await store.quick_add(" ".join(text)), store.tz

    task, tz = _task_cmd(go)
    console.print(f"[green]Added[/] {_task_line(task, tz)}")


@tasks_app.command("list")
def tasks_list(
    view: Annotated[
        str, typer.Option("--view", "-v", help="today|upcoming|open|overdue|someday|done|all")
    ] = "open",
    query: Annotated[str, typer.Option("--query", "-q", help="Words from the title")] = "",
) -> None:
    """List tasks."""

    async def go(store):  # type: ignore[no-untyped-def]
        return await store.list(view, query), store.tz

    tasks, tz = _task_cmd(go)
    for t in tasks:
        console.print(_task_line(t, tz))
    if not tasks:
        console.print("[dim]Nothing here.[/]")


@tasks_app.command("next")
def tasks_next(
    minutes: Annotated[int | None, typer.Option("--minutes", "-m", help="Time you have")] = None,
) -> None:
    """What to do now, with reasons (no AI involved)."""
    from neurovex.modules.tasks.planner import suggest

    async def go(store):  # type: ignore[no-untyped-def]
        return await store.list("open", limit=500), store.tz

    tasks, tz = _task_cmd(go)
    picks = suggest(tasks, utcnow(), tz, minutes)
    if not picks:
        console.print("[dim]No open tasks.[/]")
    for i, s in enumerate(picks):
        console.print(("[#7BA7BC]›[/] " if i == 0 else "  ") + _task_line(s.task, tz))
        console.print(f"    [dim]{'; '.join(s.reasons) or 'open'}[/]")


@tasks_app.command("done")
def tasks_done(
    task: Annotated[list[str], typer.Argument(help="Id or words from the title")],
) -> None:
    """Mark a task done. Repeating tasks get their next date."""

    async def go(store):  # type: ignore[no-untyped-def]
        done, nxt = await store.complete((await store.resolve(" ".join(task))).id)
        return done, nxt, store.tz

    done, nxt, tz = _task_cmd(go)
    console.print(f"[green]Done[/] {_task_line(done, tz)}")
    if nxt:
        console.print(f"[dim]Next:[/] {_task_line(nxt, tz)}")


@tasks_app.command("undo")
def tasks_undo(task_id: int) -> None:
    """Reopen a task you completed or dropped."""

    async def go(store):  # type: ignore[no-untyped-def]
        return await store.reopen(task_id), store.tz

    task, tz = _task_cmd(go)
    console.print(f"[green]Reopened[/] {_task_line(task, tz)}")


@tasks_app.command("move")
def tasks_move(
    task_id: int,
    when: Annotated[list[str], typer.Argument(help="e.g. fri 3pm, tomorrow, 2026-10-20, none")],
) -> None:
    """Reschedule a task ('none' clears its date)."""
    text = " ".join(when)

    async def go(store):  # type: ignore[no-untyped-def]
        due = None if text.strip().lower() == "none" else store.parse_due(text)
        return await store.update(task_id, due=due), store.tz

    task, tz = _task_cmd(go)
    console.print(f"[green]Moved[/] {_task_line(task, tz)}")


@tasks_app.command("rm")
def tasks_rm(task_id: int) -> None:
    """Delete a task for good (to keep a record, use done instead)."""
    if not _task_cmd(lambda store: store.delete(task_id)):
        console.print(f"[red]No task #{task_id}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Deleted[/] #{task_id}")


# --- llm ------------------------------------------------------------------------


def _with_router[T](fn: Callable[..., Awaitable[T]]) -> T:
    from neurovex.llm.router import LLMRouter
    from neurovex.llm.usage import db_recorder

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.model_copy(update={"log_level": "ERROR"}))

    async def main() -> T:
        engine = create_engine(settings.db_url)
        await init_db(engine)
        sessions = create_sessionmaker(engine)
        router = LLMRouter.from_settings(settings, db_recorder(sessions))
        try:
            return await fn(router, sessions)
        finally:
            await router.aclose()
            await engine.dispose()

    return asyncio.run(main())


@llm_app.command("status")
def llm_status(hours: Annotated[int, typer.Option(help="Usage window")] = 24) -> None:
    """Provider chain, keys, models and usage over the last day."""
    from neurovex.llm.usage import usage_summary

    async def go(router, sessions):  # type: ignore[no-untyped-def]
        async with sessions() as s:
            return router.status(), await usage_summary(s, hours)

    states, usage = _with_router(go)
    by_provider = {u["provider"]: u for u in usage}
    settings = get_settings()
    table = Table(title=f"AI providers · strategy {settings.llm_strategy}", expand=True)
    for col in ("#", "Provider", "Status", "Model", f"OK/{hours}h", "Failures", "Avg ms"):
        table.add_column(col, overflow="fold")
    for i, st in enumerate(states, 1):
        u = by_provider.get(st["id"], {})
        status = {
            "ready": "[green]ready[/]",
            "not_configured": f"[dim]no key ({st['key_env']})[/]",
            "disabled": f"[red]disabled: {st['disabled_reason']}[/]",
            "cooling_down": "[yellow]cooling down[/]",
        }[st["status"]]
        fails = ", ".join(f"{k} {v}" for k, v in (u.get("failures") or {}).items()) or ""
        table.add_row(str(i), st["name"], status, st["model"] or "", str(u.get("ok", 0)),
                      fails, str(u.get("avg_latency_ms") or ""))  # fmt: skip
    console.print(table)
    if not any(st["configured"] for st in states):
        console.print("[yellow]No provider has a key yet. Run `nvx llm setup`.[/]")


@llm_app.command("test")
def llm_test(
    provider: Annotated[str | None, typer.Argument(help="Provider id; all if omitted")] = None,
) -> None:
    """Send a tiny JSON request to each configured provider and report latency."""

    async def go(router, _sessions):  # type: ignore[no-untyped-def]
        if provider:
            if provider not in router.states:
                return [
                    {"provider": provider, "ok": False, "error": "not in NEUROVEX_LLM_PROVIDERS"}
                ]
            return [await router.test(provider)]
        return await router.test_all()

    with console.status("Testing AI providers..."):
        results = _with_router(go)
    if not results:
        console.print("[yellow]No provider has a key yet. Run `nvx llm setup`.[/]")
        raise typer.Exit(1)
    for r in results:
        if r["ok"]:
            pid, ms, mode = r["provider"], r["latency_ms"], r["json_mode"]
            console.print(f"[green]OK[/]   {pid:<12} {r['model']}  {ms} ms  (json: {mode})")
        else:
            console.print(f"[red]FAIL[/] {r['provider']:<12} {r.get('error')}")
    if not all(r["ok"] for r in results):
        raise typer.Exit(1)


@llm_app.command("models")
def llm_models(
    provider: str,
    search: Annotated[str | None, typer.Option("--search", "-q")] = None,
) -> None:
    """List models a provider offers right now (needs its key)."""

    async def go(router, _sessions):  # type: ignore[no-untyped-def]
        return await router.list_models(provider)

    try:
        names = _with_router(go)
    except Exception as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    if search:
        names = [n for n in names if search.lower() in n.lower()]
    for n in names:
        console.print(n)
    console.print(
        f"[dim]{len(names)} models. Set one with NEUROVEX_{provider.upper()}_MODEL=<id>[/]"
    )


@llm_app.command("setup")
def llm_setup() -> None:
    """Where to get each free API key, and how to store it."""
    from neurovex.llm.catalog import DEFAULT_PROVIDERS, PROVIDERS

    settings = get_settings()
    for pid in DEFAULT_PROVIDERS:
        spec = PROVIDERS[pid]
        names = [spec.key_env] + ([spec.account_env] if spec.account_env else [])
        have = all(settings.secret(n) for n in names)
        mark = "[green]ready[/]" if have else "[yellow]missing[/]"
        console.print(f"[bold]{spec.name}[/] {mark}")
        console.print(f"  get a key: {spec.signup_url}")
        console.print(f"  free tier: {spec.free_tier}")
        for n in names:
            console.print(f"  store it:  nvx secrets set {n}    (or env var {n} on a server)")
        console.print()
    console.print("Then check them with: [bold]nvx llm test[/]")


# --- news -----------------------------------------------------------------------


def _importance_style(n: int | None) -> str:
    if n is None:
        return "dim"
    return "bold red" if n >= 8 else "yellow" if n >= 6 else "cyan" if n >= 4 else "dim"


@news_app.command("refresh")
def news_refresh() -> None:
    """Fetch all feeds and summarize new articles now."""

    async def go(news):  # type: ignore[no-untyped-def]
        with console.status(f"Refreshing (summaries: {news.pipeline.provider_name})…"):
            return await news.pipeline.run("cli")

    run = _run_with_news(go)
    color = "green" if run.status == "OK" else "red"
    console.print(
        Panel.fit(
            f"[{color}]{run.status}[/] in {run.duration_s}s · provider {run.provider}\n"
            f"sources {run.sources_checked} checked, {run.sources_failed} failed\n"
            f"articles {run.articles_new} new, {run.articles_duplicate} duplicate, "
            f"{run.articles_summarized} summarized, {run.articles_skipped} skipped, "
            f"{run.articles_failed} failed",
            title="News refresh",
        )
    )
    if run.error:
        console.print(f"[yellow]{run.error}[/]")


@news_app.command("list")
def news_list(
    sort: Annotated[
        str, typer.Option("--sort", "-s", help="for_you | latest | importance")
    ] = "for_you",
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    topic: Annotated[str | None, typer.Option("--topic", "-t")] = None,
    min_importance: Annotated[int | None, typer.Option("--min", "-m", min=1, max=10)] = None,
    hours: Annotated[int | None, typer.Option("--hours")] = None,
    search: Annotated[str | None, typer.Option("--search", "-q")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """List articles, ranked for you by default."""
    from neurovex.modules.news import store

    if sort not in ("for_you", "latest", "importance"):
        raise typer.BadParameter("sort must be for_you, latest or importance")

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            return await store.list_articles(
                s,
                await store.load_profile(s),
                sort=sort,  # type: ignore[arg-type]
                limit=limit,
                category=category,
                topic=topic,
                min_importance=min_importance,
                since_hours=hours,
                q=search,
            )

    items, total = _run_with_news(go)
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Imp", justify="center", no_wrap=True)
    table.add_column("Title", ratio=1)
    table.add_column("Source", style="dim", no_wrap=True)
    table.add_column("When", style="dim", no_wrap=True)
    for a in items:
        imp = str(a.importance) if a.importance else "-"
        title = a.title + (f"\n[dim]{a.summary}[/]" if a.summary else "")
        if a.reasons and sort == "for_you":
            title += f"\n[#7BA7BC]+ {' / '.join(a.reasons)}[/]"
        table.add_row(str(a.id), f"[{_importance_style(a.importance)}]{imp}[/]", title,
                      a.source_name, _ago(a.published_at or a.discovered_at))  # fmt: skip
    console.print(table)
    console.print(f"[dim]{len(items)} of {total}. `nvx news show ID` for details.[/]")


@news_app.command("show")
def news_show(article_id: int) -> None:
    """Show one article's full brief."""
    from neurovex.modules.news import store

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            a = await store.get_article(s, article_id)
            return store.to_article_out(a) if a else None

    a = _run_with_news(go)
    if a is None:
        console.print(f"[red]No article {article_id}[/]")
        raise typer.Exit(1)
    body = [f"[dim]{a.source_name} · {_ago(a.published_at)} · {', '.join(a.categories or [])}[/]"]
    if a.summary:
        body += ["", a.summary]
        if a.what_happened:
            body += ["", "[bold]What happened[/]", a.what_happened]
        if a.why_it_matters:
            body += ["", "[bold]Why it matters[/]", a.why_it_matters]
        if a.key_points:
            body += ["", "[bold]Key points[/]", *[f"• {p}" for p in a.key_points]]
    else:
        body += ["", f"[yellow]{a.status}[/] {a.last_error or ''}", a.excerpt or ""]
    body += ["", f"[link={a.url}]{a.url}[/link]"]
    imp = f"[{_importance_style(a.importance)}]{a.importance or '·'}/10[/]"
    console.print(Panel("\n".join(body), title=f"{imp}  {a.title}", title_align="left"))


@news_app.command("digest")
def news_digest(
    hours: Annotated[int, typer.Option(help="Look-back window")] = 24,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 7,
) -> None:
    """The stories that matter most to you from the last day."""
    from neurovex.modules.news import store

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            return await store.digest(s, await store.load_profile(s), hours=hours, limit=limit)

    items = _run_with_news(go)
    if not items:
        console.print("[dim]Nothing summarized yet. Run `nvx news refresh`.[/]")
        return
    console.print(f"[bold #7BA7BC]Top {len(items)} for you · last {hours}h[/]\n")
    for i, a in enumerate(items, 1):
        console.print(f"[{_importance_style(a.importance)}]{a.importance:>2}[/]  "
                      f"[bold]{a.title}[/]  [dim]({a.source_name}, #{a.id})[/]")  # fmt: skip
        if a.summary:
            console.print(f"    {a.summary}")
        if a.why_it_matters:
            console.print(f"    [italic dim]Why it matters: {a.why_it_matters}[/]")
        if i < len(items):
            console.print()


@news_app.command("briefing")
def news_briefing(
    new: Annotated[bool, typer.Option("--new", help="Write a fresh briefing now")] = False,
) -> None:
    """Your personal briefing: the stories that matter to you, with a personal angle."""
    from neurovex.modules.news import briefing, store

    async def go(news):  # type: ignore[no-untyped-def]
        if new:
            with console.status(f"Writing your briefing ({news.pipeline.provider_name})..."):
                bid = await news.pipeline.make_briefing("manual")
            if bid is None:
                return None
        async with news.sessions() as s:
            latest = await store.latest_briefing(s)
            return await briefing.briefing_out(s, latest) if latest else None

    try:
        b = _run_with_news(go)
    except briefing.NotEnoughStories as e:
        console.print(f"[yellow]{e}. Run `nvx news refresh` first.[/]")
        raise typer.Exit(1) from e
    if b is None:
        console.print(
            "[dim]No briefing yet. Run `nvx news briefing --new`"
            " (needs an AI key: `nvx llm setup`).[/]"
        )
        return
    console.print(f"[dim]{_ago(b.created_at)} · {b.provider}[/]")
    console.print(f"[bold #7BA7BC]{b.headline}[/]\n")
    console.print(b.overview + "\n")
    for i in b.items:
        imp = f"[{_importance_style(i.importance)}]{i.importance or '-':>2}[/]"
        console.print(f"{imp}  [bold]{i.title}[/]  [dim]({i.source_name}, #{i.article_id})[/]")
        console.print(f"    {i.take}")
    if b.watch:
        console.print(f"\n[dim]WATCH[/]  {b.watch}")


def _set_interest(value: str, mode: str, kind: str) -> None:
    from neurovex.modules.news import store
    from neurovex.modules.news.models import InterestKind

    try:
        k = InterestKind(kind)
    except ValueError as e:
        raise typer.BadParameter("kind must be topic, category or source") from e

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            await store.set_interest(s, k, value, 1.0 if mode == "follow" else -1.0)
            await s.commit()

    _run_with_news(go)
    verb = "Following" if mode == "follow" else "Muted"
    console.print(f"[green]{verb}[/] {kind} [bold]{value}[/]")


@news_app.command("follow")
def news_follow(
    value: str,
    kind: Annotated[str, typer.Option(help="topic | category | source (source = id)")] = "topic",
) -> None:
    """Follow a topic (e.g. Rust), category (e.g. AI) or source id: ranked higher."""
    _set_interest(value, "follow", kind)


@news_app.command("mute")
def news_mute(
    value: str,
    kind: Annotated[str, typer.Option(help="topic | category | source (source = id)")] = "topic",
) -> None:
    """Mute a topic, category or source: hidden from your feed."""
    _set_interest(value, "mute", kind)


@news_app.command("unfollow")
def news_unfollow(value: str) -> None:
    """Remove a follow or mute by its value."""
    from sqlalchemy import delete

    from neurovex.modules.news.models import Interest
    from neurovex.modules.news.personalize import norm

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            result = await s.execute(
                delete(Interest).where(
                    (Interest.value_key == norm(value)) | (Interest.value_key == value.strip())
                )
            )
            await s.commit()
            return result.rowcount or 0

    n = _run_with_news(go)
    console.print(
        f"[green]Removed[/] {value}" if n else f"[yellow]Not following or muting {value}[/]"
    )


@news_app.command("interests")
def news_interests() -> None:
    """What you follow, mute, and what Neurovex has learned you like."""
    from neurovex.modules.news import store

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            return await store.load_profile(s), await store.interaction_count(s)

    profile, n = _run_with_news(go)
    follow = (
        ", ".join(f"{r.value} [dim]({r.kind})[/]" for r in profile.follows) or "[dim]nothing[/]"
    )
    mute = ", ".join(f"{r.value} [dim]({r.kind})[/]" for r in profile.mutes) or "[dim]nothing[/]"
    console.print(f"[bold]Following:[/] {follow}")
    console.print(f"[bold]Muted:[/] {mute}")
    likes = profile.top_learned(10)
    dislikes = profile.top_learned(5, positive=False)
    console.print(f"[bold]Learned from {n} interactions:[/]")
    for _, label, a in likes:
        console.print(f"  [green]+{a:.2f}[/] {label}")
    for _, label, a in dislikes:
        console.print(f"  [red]{a:.2f}[/] {label}")
    if not likes and not dislikes:
        console.print("  [dim]nothing yet. Open, save, or use more/less in the web UI.[/]")


@sources_app.command("list")
def sources_list() -> None:
    """List feeds."""
    from neurovex.modules.news import store

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            return await store.list_sources(s)

    table = Table(show_header=True, header_style="bold")
    for col in ("ID", "On", "Name", "Category", "Articles", "Checked", "URL / error"):
        table.add_column(col)
    for src in _run_with_news(go):
        detail = f"[red]{src.last_error}[/]" if src.last_error else f"[dim]{src.url}[/]"
        table.add_row(str(src.id), "✓" if src.enabled else "·", src.name, src.category or "",
                      str(src.article_count), _ago(src.last_checked_at), detail)  # fmt: skip
    console.print(table)


@sources_app.command("add-x")
def sources_add_x(
    handle: Annotated[str, typer.Argument(help="e.g. @OpenAI or x.com/OpenAI")],
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
) -> None:
    """Follow a public X (Twitter) account, for free. Posts arrive on the next refresh."""
    from neurovex.modules.news import store
    from neurovex.modules.news.models import Source
    from neurovex.modules.news.xsource import parse_handle, source_url

    name = parse_handle(handle)
    if name is None:
        raise typer.BadParameter("Use @name or an address like x.com/name")

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            if await store.get_source_by_url(s, source_url(name)):
                return False
            s.add(Source(name=f"@{name}", url=source_url(name), category=category))
            await s.commit()
        return True

    if _run_with_news(go):
        console.print(f"[green]Added[/] @{name}. Its posts arrive on the next refresh.")
    else:
        console.print(f"[yellow]@{name} is already added.[/]")


@sources_app.command("add")
def sources_add(
    name: str,
    url: str,
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
) -> None:
    """Add an RSS/Atom feed."""
    from neurovex.modules.news import fetcher, store
    from neurovex.modules.news.models import Source
    from neurovex.modules.news.urls import is_http_url

    if not is_http_url(url):
        raise typer.BadParameter("URL must be http(s)")

    async def go(news):  # type: ignore[no-untyped-def]
        result = await fetcher.fetch_feed(news.http, url)  # validate before saving
        async with news.sessions() as s:
            if await store.get_source_by_url(s, url):
                return None, len(result.entries)
            s.add(Source(name=name, url=url, category=category))
            await s.commit()
        return name, len(result.entries)

    try:
        added, n = _run_with_news(go)
    except Exception as e:
        console.print(f"[red]Could not read that feed:[/] {e}")
        raise typer.Exit(1) from e
    if added:
        console.print(f"[green]Added[/] {name}: {n} entries in the feed right now.")
    else:
        console.print("[yellow]That feed is already added.[/]")


def _set_source(source_id: int, **changes: object) -> None:
    from neurovex.modules.news.models import Source

    async def go(news):  # type: ignore[no-untyped-def]
        async with news.sessions() as s:
            src = await s.get(Source, source_id)
            if src is None:
                return None
            if changes.get("_delete"):
                await s.delete(src)
            else:
                for k, v in changes.items():
                    setattr(src, k, v)
            await s.commit()
            return src.name

    name = _run_with_news(go)
    if name is None:
        console.print(f"[red]No source {source_id}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Updated[/] {name}")


@sources_app.command("enable")
def sources_enable(source_id: int) -> None:
    """Enable a feed."""
    _set_source(source_id, enabled=True)


@sources_app.command("disable")
def sources_disable(source_id: int) -> None:
    """Disable a feed (keeps its articles)."""
    _set_source(source_id, enabled=False)


@sources_app.command("remove")
def sources_remove(source_id: int) -> None:
    """Remove a feed and its articles."""
    _set_source(source_id, _delete=True)
