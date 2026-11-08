# Neurovex

Personal AI operating system (JARVIS-style) for one user on Windows 11. Local-first.
Read `PLAN.md` (roadmap), `docs/ARCHITECTURE.md` (how it's built), `docs/DECISIONS.md` (why), `docs/PROGRESS.md` (where we are).

## Hardware constraints
Ryzen 9 9900X, **no discrete GPU**, **15 GB RAM**. AI runs on **Puku AI only** (the user's choice, D-026) through `neurovex/llm`, using its `/v1/messages` endpoint. Other providers (Groq, Cerebras, Gemini, Mistral, OpenRouter, Cloudflare, Hugging Face, Claude, Ollama) exist in the catalog but are off unless added to `NEUROVEX_LLM_PROVIDERS`; don't add new ones unasked. Must also run on a production server (Linux, env-var secrets).

## Commands (Python via uv; `python -m uv` works if `uv` isn't on PATH)
- In PowerShell without an activated venv, use the launcher: `.\nvx <command>` (runs `.venv\Scripts\nvx.exe`)
- `uv run nvx serve` → API + web UI at http://127.0.0.1:8765
- `uv run nvx doctor` → environment checks
- `uv run nvx autostart on | off | status` → start at Windows sign-in with no window (Task Scheduler; output in `<data_dir>/logs/server.log`)
- `uv run nvx backup` → back up the database now (it also backs up daily on its own, `core/backup.py`)
- `uv run nvx news refresh | list [--sort for_you|latest|importance] | show ID | digest | briefing [--new]`
- `uv run nvx news follow X | mute X | unfollow X | interests` (personal layer) · `nvx news sources list|add|add-x|enable|disable|remove`
- `uv run nvx chat ["question"]` → Ask Neurovex (interactive, or one-shot)
- `uv run nvx today` → the day at a glance (no AI)
- `uv run nvx memory list | search Q | add TEXT [--kind --pin] | forget ID | clear`
- `uv run nvx tasks` (overview) `| add TEXT | list [--view] | next [--minutes] | done ID-or-words | undo ID | move ID WHEN | rm ID`
- `uv run nvx llm setup | status | test [id] | models <id>` → AI provider keys, health, live test, model lists
- `uv run nvx secrets set PUKU_API_KEY` (etc.) → Windows Credential Manager; servers use env vars + `NEUROVEX_USE_KEYRING=false`
- `uv run pytest` · `uv run ruff check .` · `uv run ruff format .`
- On this machine set `UV_LINK_MODE=copy` (cache and project are on different drives). If the editor activated `.venv`, `python -m uv` fails; use `.venv/Scripts/ruff`, `.venv/Scripts/python -m pytest`, or the system Python for uv.

## Layout
- `neurovex/core/` config (pydantic-settings, `NEUROVEX_` env prefix), logging (structlog + secret redaction), db (SQLite WAL, `UTCDateTime` type)
- `neurovex/api/guard.py` loopback Host check (plus `NEUROVEX_ALLOWED_HOSTS`, e.g. a Tailscale name) + `X-Neurovex-Client` header required on writes (until token auth, P0.3)
- `neurovex/core/backup.py` daily SQLite backups (keep 7) · `neurovex/core/autostart.py` Windows sign-in task
- `neurovex/core/skills.py` skill registry: Pydantic params, risk levels (destructive/external/exec need confirmation), never raises
- `neurovex/llm/` AI layer: `catalog.py` (provider specs + defaults + rate hints), `providers.py` (OpenAI-compatible client with JSON-mode downgrade, `MessagesProvider` for Anthropic-format gateways like Puku, Anthropic SDK, Ollama), `ratelimit.py` (`RateGate`: rpm/tpm window + learns x-ratelimit-* headers), `router.py` (priority/round-robin, pacing with `max_wait_s`, `reserve_tokens` headroom for background work, cooldowns, health), `usage.py` (`llm_calls` table), `api.py` (/api/llm)
- `neurovex/conversation/` Ask Neurovex: `engine.py` (provider-neutral JSON step protocol: use_tool | respond, max 4 steps; `then` = more calls in one step; a reply on a change-only step ends the turn; full tool signatures only for the groups a request needs, via `SkillRegistry.hint`), `prompts/` (persona, protocol), `builtins.py`, `api.py` (/api/chat), tables `chat_*`. App assembly (skills from each module) in `main.build_chat`.
- `neurovex/memory/` what Neurovex knows about the user: `memories` table + FTS5 index (`core.db.ensure_fts`), `store.py` (CRUD, dedupe, secret filter, BM25 search, past-chat search), `recall.py` (budgeted `<memory>` block: instructions + preferences + pinned + importance-3, then keyword-relevant), `skills.py`, `api.py` (/api/memory). Writes ride on chat steps (`memory` ops), never a separate LLM call.
- `neurovex/core/clock.py` the user's local time (`NEUROVEX_TIMEZONE`, else this computer's); `neurovex/core/live.py` SSE broadcaster, merged for pages at `/api/events`
- `neurovex/modules/tasks/` to-dos: `dates.py` (quick-add and date parsing, deterministic), `planner.py` (buckets, labels, the "what now" advisor, pure functions, no AI), `store.py`, `module.py` (reminder loop), `skills.py`, `api.py` (/api/tasks)
- `neurovex/today/` home screen, above the modules like `conversation` (may read from all of them): `brief.py` (composes tasks, news, memory; no AI calls), `module.py` (once-a-day announcement), `skills.py`, `api.py` (/api/today)
- `neurovex/modules/news/` first module: fetcher → pipeline → summarizer (`RouterSummarizer` → `neurovex.llm` router) → store → api/module; `skills.py` exposes news to chat
  - personal layer: `personalize.py` (scoring + learning, pure functions), `briefing.py`, `catalog.py` (verified feeds)
  - `xsource.py` public X accounts as sources; checked by how recently each posted (`check_every`), with each run taking only its share (`pick_due`), free (no key, no login, no paid X API; D-030): FxTwitter's API finds new posts; `xtf` (x-tweet-fetcher, pinned to a reviewed commit) reads X Articles in full and timelines through an optional Nitter (`NEUROVEX_X_NITTER_URL`). Never use its browser backend
  - schema stopgap: `core/db.py::_add_missing_columns` adds new *nullable* columns on start
- `neurovex/web/index.html` single-file UI (vanilla JS; render untrusted text with `textContent` only). Six sections in a side rail (bottom bar on phones): Today, News (with sub-tabs), Tasks, Ask, Memory, Settings; a section with its own view goes in `VIEWS`. Fetch only what the current section shows (`applyTabView`, `refreshVisible`); never replace the feed while it's being read; long lists search or cap instead of rendering everything. Dark by default; theme tokens on `:root` plus light copies. Check dark and light, and 1400 / 850 / 390 px
- `tests/` offline: feeds mocked with respx, LLM faked. The anthropic 1.x SDK uses httpx2, so respx can't intercept it; inject a fake client instead.

## Rules
- **Free only:** the user's plan is fully free. Never add anything that needs payment; if the only route costs money, say so instead of building it.
- Async everywhere; blocking work through `asyncio.to_thread`.
- Modules under `neurovex/modules/` don't import each other; only `conversation`, `today` and `main` may combine them. Timestamps are timezone-aware UTC.
- Never log secrets. Article/web content is untrusted: fence it for LLMs, never render it as HTML.
- New capabilities = skills (`core/skills.py`) registered in `main.build_chat`, so chat can use them.
- New LLM work goes through `LLMRouter.complete_json(JSONRequest, parse)`; background work passes `max_wait_s` + `reserve_tokens`, interactive work a short `max_wait_s`; never call a provider SDK directly from a module. `parse` must raise on bad output so the router fails over.
- Memory must stay zero-extra-call: recall is local (FTS5), writes ride on existing LLM calls. Keep memories atomic (one fact each); never store secrets (`memory.store.looks_secret`).
- Chat prompts are resent on every step: keep tool descriptions short, and give a new module's skills hint words (`skills.hint`) in `main.build_chat`. Size `max_tokens` to real output: some providers charge it up front.
- `ProvidersExhausted.kinds` all transient (rate limit/outage/keys) → not the input's fault: don't consume retries.
- Anthropic SDK 1.x: no `temperature`; use `output_config={"format": ..., "effort": ...}`; forced `tool_choice` is rejected on current models.
- Schema is `create_all` for now; switch to Alembic when the P0.1 scaffold lands.
