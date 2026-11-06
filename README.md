# Neurovex

A personal AI operating system for one user. It runs locally on Windows 11 and is built to run on a Linux server too.

It collects AI news from RSS feeds and public X accounts, summarizes and ranks it for you, manages your tasks, remembers what you tell it, and answers questions using all of that.

## What's in it

| Section | What it does |
|---|---|
| **Today** | The day at a glance: tasks, top news and a daily brief |
| **News** | Feeds and X accounts, AI summaries, personal ranking and briefings |
| **Tasks** | Quick-add to-dos with dates, reminders and a "what now" advisor |
| **Ask** | Chat that can use your news, tasks and memory |
| **Memory** | Facts and preferences, searched locally with no extra AI calls |
| **Settings** | Sources, AI provider status and preferences |

## Getting started

You need [uv](https://docs.astral.sh/uv/) and Git. uv installs Python 3.12 if you don't have it.

```powershell
uv sync
uv run nvx secrets set PUKU_API_KEY
uv run nvx doctor
uv run nvx serve
```

Then open http://127.0.0.1:8765.

On a server, set keys as environment variables and add `NEUROVEX_USE_KEYRING=false`. `.env.example` lists every setting.

Neurovex has no login yet, so don't expose it to the internet. It only accepts local connections by default.

## Common commands

```powershell
uv run nvx today                # the day at a glance
uv run nvx chat "question"      # ask Neurovex
uv run nvx news refresh         # fetch and summarize news now
uv run nvx tasks                # task overview
uv run nvx memory list          # what Neurovex remembers
uv run nvx llm status           # AI provider health
```

## Project layout

```text
neurovex/
  core/          config, logging, database, clock, live events, skill registry
  api/           local request protection
  llm/           AI providers, routing, rate limits, usage log
  modules/news/  feeds, X accounts, summaries, personal ranking, briefings
  modules/tasks/ to-dos, date parsing, planner, reminders
  memory/        stored facts and local search
  conversation/  the chat engine
  today/         the home screen, combining the modules
  web/           the single-file web interface
  main.py        app assembly
  cli.py         the nvx command
tests/           offline tests with mocked feeds and a fake AI
docs/            architecture, decisions, progress
```

## Development

```powershell
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Documentation

- [PLAN.md](PLAN.md): the roadmap
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it's built
- [docs/DECISIONS.md](docs/DECISIONS.md): why it's built this way
- [docs/PROGRESS.md](docs/PROGRESS.md): where it stands
- [CLAUDE.md](CLAUDE.md): working rules for AI coding assistants
