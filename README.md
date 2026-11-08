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

Then sign this browser in:

```powershell
uv run nvx signin    # prints a one-time link; open it
```

On a server, set keys as environment variables and add `NEUROVEX_USE_KEYRING=false`. `.env.example` lists every setting.

## Routines

Teach Neurovex a phrase in Ask: "When I say *start my day*, show my day and what to do next." After that, saying "start my day" runs those steps at once, with no AI call. Words after the phrase can fill a step: teach "when I say *add groceries*, add a task to buy what I say", then say "add groceries oat milk".

Routines are listed in **Settings → Routines**, where you can run or delete them, and with `.\nvx routines`. A routine stops at any step that fails or needs your confirmation.

## Signing in

Every browser signs in once. On this PC, run `.\nvx signin` and open the link it prints. To add a phone, open **Settings → Devices** on a signed-in device, choose **Sign in another device**, and type the code on the phone. Codes work once, for 10 minutes. The same panel lists signed-in devices and signs any of them out.

Scripts use the API token instead: `.\nvx token` prints it, to send as `Authorization: Bearer <token>`. `.\nvx token --new` replaces it.

Everything chat does, and every sign-in, is recorded in a tamper-evident audit log. `.\nvx audit tail` shows the latest entries, and `.\nvx audit verify` checks that none were changed or removed.

## Keep it running

```powershell
.\nvx autostart on       # start at every sign-in, with no window (and start it now)
.\nvx autostart status
.\nvx autostart off      # stop it and don't start at sign-in
```

Output goes to `%LOCALAPPDATA%\Neurovex\logs\server.log`. It stops while the PC sleeps. On Linux, `nvx autostart on` prints a systemd unit to use instead.

## Backups

The database is copied every day to `%LOCALAPPDATA%\Neurovex\backups`, and the last 7 copies are kept. Run `.\nvx backup` to make one now. To keep copies off this PC, set `NEUROVEX_BACKUP_DIR` to a synced folder such as OneDrive.

To restore, stop Neurovex, copy a backup over `neurovex.db` in the data folder, delete the `-wal` and `-shm` files next to it, and start Neurovex again.

## Use it from your phone (Tailscale)

Keep Neurovex off the open internet: sign-in protects it, but there's no need to expose it. [Tailscale](https://tailscale.com) is free for personal use and connects only your own devices.

1. Install Tailscale on this PC and your phone, and sign in to both with the same account.
2. In the Tailscale admin console, under DNS, turn on MagicDNS and HTTPS certificates.
3. On this PC, run `tailscale serve --bg 8765`. It prints your address, such as `https://pc.tail1234.ts.net`.
4. Put that name in `.env` as `NEUROVEX_ALLOWED_HOSTS=pc.tail1234.ts.net`, then restart Neurovex with `.\nvx autostart off` and `.\nvx autostart on`.
5. Open the address on your phone and sign in with a code from **Settings → Devices**. Only devices signed in to your Tailscale account can reach it at all.

If a page says "Host not allowed", the message names the address to add.

## Common commands

```powershell
uv run nvx today                # the day at a glance
uv run nvx chat "question"      # ask Neurovex
uv run nvx news refresh         # fetch and summarize news now
uv run nvx tasks                # task overview
uv run nvx memory list          # what Neurovex remembers
uv run nvx llm status           # AI provider health
uv run nvx backup               # back up the database now
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
