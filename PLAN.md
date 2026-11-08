# NEUROVEX — Master Plan

> A personal AI operating system. JARVIS, but real, running on your own PC.
> Plan version 1.0 · 2026-10-09 · Status: pre-build

**The documents in this plan**

| File | What it's for |
|---|---|
| `PLAN.md` (this file) | Vision, rules, roadmap, timeline, risks. Start here. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How Neurovex is built: processes, data flow, memory, LLM routing, security, repo layout. |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every place this plan departs from the original "Mirror" prompt pack, and why. |
| [docs/PROMPTS.md](docs/PROMPTS.md) | The rewritten build prompts, one per milestone, ready to paste into your AI coding assistant. |

---

## 1. What Neurovex is

Neurovex is a single, always-on intelligence that lives on your PC and is built for exactly one person: you. It is not a chatbot you open. It is a presence that:

| Pillar | Meaning | Delivered in |
|---|---|---|
| **Converses** | Talk to it by text or voice, like a person who knows you | Phase 1, 4 |
| **Remembers** | Remembers what you said, what you did, what you like, and how you work, across every session | Phase 2 |
| **Organizes** | Runs your tasks, your routines, your body's signals | Phase 5 |
| **Notices** | Watches context (with consent) and speaks up at the right moment, not randomly | Phase 6, 7 |
| **Acts** | Uses a browser, files and code on your behalf, never without you knowing | Phase 8 |
| **Protects** | Your most private data, encrypted, auditable, deletable | Phase 0 → 9 |

**One-line success test:** *you use Neurovex every day for two weeks without being forced to, and you'd miss it if it was gone.*

---

## 2. Ground rules (the principles every decision follows)

1. **Always runnable.** From the end of week 2 there is always a working Neurovex you can talk to. Each phase adds to something that already works. No "it'll all come together in sprint 8".
2. **Your data stays local. Thinking can go to the cloud.** All memory, logs and personal data live on your disk. Cloud LLM calls get only the minimum context needed, and you can see what was sent.
3. **Nothing acts without you knowing.** Anything with a side effect outside Neurovex (send, buy, submit, delete, execute) is announced, confirmable, logged, and reversible where possible.
4. **Security is built in from day one**, not saved for the final sprint. Secrets in the OS keychain from Phase 0. Permissions and the audit log from Phase 0. Encryption at rest hardened in Phase 9.
5. **Measure everything that matters.** Latency on every step, cost on every LLM call, and a golden test set for routing and memory. If it isn't measured, it isn't done.
6. **Simple first, clever later.** One process, one database, no Docker needed to run. Add infrastructure only when a real limit is hit.
7. **Every phase ends with a demo you'd actually use.** If a phase's exit demo isn't useful to you, re-scope the phase.

---

## 3. Your machine, and what it means for Neurovex

Detected on 2026-10-09:

| Component | Detected | Implication |
|---|---|---|
| CPU | AMD Ryzen 9 9900X (12 cores / 24 threads) | Excellent. Fast enough for CPU speech-to-text, CPU text-to-speech, embeddings and small local LLMs. |
| GPU | AMD Radeon integrated graphics (512 MB) | **No usable GPU for AI.** Treat all local inference as CPU-only. |
| RAM | 15 GB | **The tightest constraint.** Everything that runs at the same time must fit: Windows + browser + Neurovex + speech models + a local LLM. |
| OS | Windows 11 Pro | Plan is Windows-first. BitLocker is available for disk encryption. |
| Python | 3.11.9 | Fine, but we'll let `uv` install 3.12 for the project. |
| Node | v25.9.0 | Fine for the frontend. |
| Ollama | Installed, has `qwen2.5` (4.7 GB) and `qwen3.5` (6.6 GB) | Usable, but a 6.6 GB model is too heavy to keep loaded next to everything else. |
| Docker | Installed | **Optional.** Neurovex won't need it to run. Docker on 15 GB RAM would eat memory we need for models. |
| Rust / cargo | **Missing** | Needed for the Tauri desktop shell in Phase 3. Install before then. |
| uv | **Missing** | Install in Phase 0. |

### What this changes compared to the original plan

The original plan assumed a GPU and aimed to run **~75% of tasks locally** on 8B–14B models. On this hardware that would be slow, especially for voice where every second counts. So Neurovex uses a **split-brain** model:

- **Cloud (Claude) is the main conversational brain.** Use Haiku 5.5 for fast everyday replies, Sonnet 5.5 for reasoning and planning, Opus 5.5 for rare heavy lifting. Streaming makes it feel instant.
- **Local (Ollama, CPU) is the private and background brain.** Embeddings, memory tagging, clipboard and window-title summarization, classification, offline fallback. These are short prompts where a small 3–4B-class model on a 9900X is fast enough, and where privacy matters most.
- **Speech runs fully local on CPU.** faster-whisper (int8) for speech-to-text and Kokoro-82M for text-to-speech are both light enough.

### RAM budget (what runs at the same time)

| Item | Approx. RAM |
|---|---|
| Windows + browser + VS Code | 5–7 GB |
| Neurovex core (Python, SQLite, API) | 0.3–0.6 GB |
| Embedding model (e.g. `nomic-embed-text`) | ~0.3–0.6 GB |
| Speech-to-text (faster-whisper `small.en` int8) | ~0.5–1 GB |
| Text-to-speech (Kokoro-82M) | ~0.3–0.6 GB |
| Local LLM, small (3–4B class, Q4) | ~2.5–3.5 GB |
| **Total** | **~9–13 GB of 15 GB** |

A 6–8 GB local model would push this past the limit. Rule: **one small local model is kept warm; bigger ones load on demand only.**

### Recommended upgrades (optional, in order of value)

1. **RAM to 32 GB.** Cheap, and removes the biggest constraint. Lets you keep an 8B local model warm.
2. **An NVIDIA GPU with 12–16 GB VRAM.** Turns local LLM + Whisper into GPU workloads, makes "mostly local" realistic. The architecture already supports flipping the routing policy, so no code rewrite is needed. Only a config change.

---

## 4. What changed from the original "Mirror" prompt pack

The original pack is a strong vision. This plan keeps the vision and all the modules but fixes the build order, the infrastructure and a few technical choices. Full reasoning is in [docs/DECISIONS.md](docs/DECISIONS.md).

| Area | Original | Neurovex plan | Why, in one line |
|---|---|---|---|
| Name | Mirror | **Neurovex** (package `neurovex`, CLI `nvx`, wake word "Hey Neurovex") | Your project. |
| Build order | UI in sprint 8, security in sprint 9 | Usable chat by week 4, a desktop UI by week 9, security baseline in week 1 | Stay motivated, and never retrofit security. |
| Infrastructure | Docker: Postgres + Redis + ChromaDB + SQLite | **One SQLite file** (+ `sqlite-vec` for vectors). No Docker needed. | 15 GB RAM, single user, one less thing to break. |
| Event bus | Redis Streams | In-process async bus + SQLite event log (same persistence and replay) | Same benefits, no server. Redis can be swapped in later behind the same interface. |
| Routing | Classify into intent enum, then search skills | **Tool-calling agent loop**, plus a fast path for workflows and commands | Modern LLMs choose tools better than a hand-built classifier does. |
| Local LLM role | ~75% of work, 8–14B models | Background, privacy and offline work, 3–4B class on CPU | No GPU. |
| Speech-to-text | `openai-whisper` + `webrtcvad` | `faster-whisper` (int8) + Silero VAD | About 4× faster on CPU. `webrtcvad` is painful to install on Windows. |
| Wake word | Porcupine "Hey Mirror" | **Push-to-talk hotkey first**, then openWakeWord (or Porcupine) "Hey Neurovex" | Ship voice sooner. A custom wake word needs training and tuning. |
| Audio I/O | `pyaudio` | `sounddevice` | Installs cleanly on Windows. |
| Frontend | Next.js 14 + Tauri | **Vite + React + TypeScript in Tauri 2** | Tauri needs a static frontend. Next.js adds nothing there. |
| Daemon | Separate process via Redis | Background tasks inside the core process. **Voice** is the separate process. | Fewer moving parts. Voice is isolated because it's heavy and crash-prone. |
| DB encryption | Hand-rolled AES-GCM on SQLite files | OS keychain for secrets now, SQLCipher for the DB in Phase 9 (verified by a spike in Phase 0) | Don't hand-roll database encryption. |
| Extensibility | Custom skills only | Custom skills **+ MCP** (Neurovex can use MCP servers for calendar, email and so on) | Reuse a huge ecosystem instead of writing every integration. |
| Timeline | 6–10 weeks | **~26 weeks** at 10–15 h/week | Honest estimate for this scope as a solo builder. |

---

## 5. Stack at a glance

| Layer | Choice |
|---|---|
| Language / tooling | Python 3.12 · `uv` · `ruff` · `pyright` · `pytest` + `pytest-asyncio` |
| API | FastAPI + Uvicorn (REST + WebSocket), bound to `127.0.0.1` with a local auth token |
| Config | `pydantic-settings` (`.env` + environment), profiles: `dev` / `test` / `prod` |
| Logging | `structlog` JSON logs with `request_id` and `turn_id` context |
| Storage | SQLite (WAL mode) through SQLAlchemy 2.0 async + `aiosqlite`, Alembic migrations |
| Vectors | `sqlite-vec` inside the same database |
| Scheduling | APScheduler 3.x with a SQLite job store |
| Cloud LLM | Anthropic SDK: `claude-haiku-5-5` · `claude-sonnet-5-5` · `claude-opus-5-5` |
| Local LLM | Ollama (CPU): one small chat model kept warm, plus `nomic-embed-text` for embeddings |
| Speech-to-text | `faster-whisper` (`small.en`, int8, CPU) + Silero VAD |
| Text-to-speech | Kokoro-82M (local) · ElevenLabs (optional premium) |
| Wake / talk | Push-to-talk global hotkey → openWakeWord custom "Hey Neurovex" |
| Agents | Playwright (browser), sandboxed subprocess (code), MCP client (`mcp` SDK) |
| Desktop | Tauri 2 + Vite + React + TypeScript + Tailwind · TanStack Query · Zustand · Motion · Recharts · d3-force |
| Secrets | `keyring` (Windows Credential Manager) |
| Crypto | `cryptography` (AES-GCM), `argon2-cffi`, SQLCipher (Phase 9) |

---

## 6. Roadmap

Ten phases. Each has a **goal**, a **demo that proves it's done**, and a checklist. The prompts for each milestone are in [docs/PROMPTS.md](docs/PROMPTS.md).

```
Wk  1-2   P0 FOUNDATIONS     ████
Wk  3-4   P1 BRAIN               ████          ◄ "It talks"
Wk  5-7   P2 MEMORY                  ██████    ◄ "It remembers"
Wk  8-9   P3 FACE                          ████          ◄ "It has a face"
Wk 10-12  P4 VOICE                             ██████    ◄ "It listens and speaks"
Wk 13-16  P5 LIFE & TASKS                            ████████   ◄ "It's useful"
Wk 17-18  P6 AMBIENT                                         ████   ◄ "It's alive"
Wk 19-20  P7 RITUALS                                             ████   ◄ "It knows me"
Wk 21-24  P8 HANDS                                                   ████████  ◄ "It acts"
Wk 25-26  P9 HARDEN & SHIP                                                   ████ ◄ "It's trustworthy"
```

### Early module: News ✅ (built first, 2026-10-09)
**Goal:** A useful feature on day one: a personal tech-news feed that summarizes and ranks stories (inspired by the Aetherix project). See [docs/DECISIONS.md](docs/DECISIONS.md) D-021.
**Exit demo:** `uv run nvx serve` → http://127.0.0.1:8765 shows ranked, summarized stories from 7 feeds. `nvx news digest` prints the top stories of the day.

- [x] Feed fetching (conditional GET), URL + title dedupe, full-text extraction for thin excerpts
- [x] Summaries with Claude (structured outputs) or local Ollama; relevance filter; importance 1–10
- [x] SQLite storage, retries, retention; REST API; web UI; `nvx news` CLI; 34 tests
- [x] Personal layer: "For you" ranking, follow/mute, learning from feedback, personal briefings, live updates, breaking alerts, feed catalog (58 tests)
- [x] AI layer on 7 free cloud providers (Cerebras, Groq, Gemini, Mistral, OpenRouter, Cloudflare, Hugging Face) with failover, cooldowns, usage log and `nvx llm`, replacing local Ollama for production (D-022, 99 tests)
- [x] Groq → Puku default chain with proactive rate pacing (no more 429 storms), chat priority over background work (D-023)
- [x] Puku only by default, through its `/v1/messages` endpoint (D-026)
- [x] Follow public X accounts for free, through x-tweet-fetcher and FxTwitter (D-030)
- [x] **Ask Neurovex**: chat in the web UI (Ask tab) and `nvx chat`, using skills over the news module (D-024). First slice of Phase 1 "It talks"
- [ ] Fold into the module system once P0.2 lands (manifest, events); skills already exist (`core/skills.py`)

### Phase 2 · Memory: first version ✅ (2026-10-14, D-025)
- [x] Remembers you across conversations (facts, preferences, people, goals, events, standing instructions), written during normal replies at no extra AI cost
- [x] Local full-text recall (FTS5): budgeted `<memory>` block per turn, plus search over past conversations
- [x] Atomic memories, smart updates, duplicate merging, secret filter
- [x] Memory tab (view/search/add/edit/pin/forget) + `nvx memory` CLI + `/api/memory`
- [x] Routines you teach it ("when I say 'start my day', do X then Y"), the procedural memory from ARCHITECTURE §9 (D-034)
- [ ] Embedding search for synonyms (only if keyword recall proves too narrow)

### Phase 7 · Today: first version ✅ (2026-11-04, D-029)
- [x] "Today" home screen: greeting, the day in a sentence, do next, overdue / today / tomorrow / done, briefing, top stories, goals, quick-add
- [x] `nvx today`, `/api/today`, chat (`today.brief`), all with no extra AI calls
- [x] Once-a-day announcement on open pages (`NEUROVEX_TODAY_BRIEF_TIME`)
- [ ] Evening reflection (interactive), weekly review, insights store

### Phase 5 · Tasks: first version ✅ (2026-11-04, D-027)
- [x] Tasks with due dates, priorities, tags, estimates and repeats; plain-words quick-add with no AI
- [x] "What should I do now?" advisor (ranked, with reasons, fits the time you have, spots repeated postponing), no AI
- [x] Reminders on open pages (toast + desktop notification), snooze, undo
- [x] Tasks tab, `nvx tasks`, `/api/tasks`, chat skills
- [ ] Inbox triage, weekly plan, focus sessions / Pomodoro
- [ ] Life OS (sleep, energy, mood logs)

### Phase 0: Foundations (weeks 1–2)
**Goal:** A clean, tested skeleton with security and observability built in.
**Exit demo:** `nvx doctor` reports green for config, database, Ollama, Anthropic key and keychain. `GET /health` returns module status. Tests pass.

- [ ] Install `uv`, then `git init`, `.gitignore`, `pre-commit` with ruff and pyright
- [ ] Project scaffold: `neurovex/` package, `pyproject.toml`, `Makefile` (or `justfile`), `.env.example`
- [ ] `config.py` with profiles (`dev`/`test`/`prod`) and validation; data dir at `%LOCALAPPDATA%\Neurovex`
- [ ] `logging.py` with structlog JSON logs and `request_id`/`turn_id` contextvars
- [ ] Database: SQLAlchemy async engine, WAL mode, Alembic, first migration
- [ ] **Spike:** confirm `sqlite-vec` loads on your Python build, and that it works under SQLCipher (decides the Phase 9 encryption path)
- [ ] Event bus (in-process, persisted to the `events` table, with dead-letter after 3 retries)
- [ ] Skill registry (Pydantic-typed params → JSON Schema; risk level per skill)
- [ ] Module base class + manifest (id, permissions, subscriptions, skills) + module loader
- [ ] Security baseline: secrets via `keyring`, permission checks, hash-chained audit log, localhost auth token
- [ ] FastAPI app with lifespan, `/health`, CORS locked to the Tauri origin
- [ ] `nvx` CLI (Typer): `nvx serve`, `nvx doctor`, `nvx secrets set`
- [ ] `CLAUDE.md` / project brief so every AI session starts with context

### Phase 1: Brain (weeks 3–4), "It talks"
**Goal:** Talk to Neurovex in a terminal. It can call tools, it streams, and it tracks cost.
**Exit demo:** `nvx chat` → "what time is it and add 'buy milk' to my notes" → it calls two tools, streams the answer, and `nvx costs` shows the spend.

- [ ] LLM provider interface + `AnthropicProvider` + `OllamaProvider` + `MockProvider` (for tests)
- [ ] Streaming, tool calling and embeddings on both providers
- [ ] Routing policy (task type → tier/model), offline fallback, budget caps (daily/monthly)
- [ ] Cost tracker (per call, per module) + `GET /api/costs`
- [ ] Prompt caching for the stable system prompt prefix
- [ ] Neurovex persona + system prompt builder
- [ ] Conversation engine: fast path (slash commands, workflow triggers) → agent loop with tools → confirmation gate
- [ ] `/ws/chat` WebSocket protocol (token stream, tool events, confirmations)
- [ ] `nvx chat` terminal client (Rich), plus 3 built-in skills: time/date, notes, system info
- [ ] Golden routing test set (start with 30 utterances, grow to 100)

### Phase 2: Memory (weeks 5–7), "It remembers"
**Goal:** Neurovex remembers you across sessions, and you can see and edit everything it knows.
**Exit demo:** Tell it "I'm learning Rust and I hate morning meetings." Restart. Ask "what do you know about me?" Correct one fact. Ask about "last Tuesday".

- [ ] Working memory (per session, token-aware, with rolling summary)
- [ ] Episodic store (SQLite + `sqlite-vec`), recall by meaning + time + importance
- [ ] Semantic store: a facts graph with provenance, confidence and `valid_from`/`valid_to` (facts can change)
- [x] Procedural store: workflows (teach mode: "when I say X, do Y then Z"), fuzzy triggers (built early as routines, D-034)
- [ ] Memory Nexus: post-turn extraction (background), retrieval scoring, context injection under a token budget
- [ ] Nightly consolidation: dedupe facts, decay importance, summarize the day
- [ ] Memory API (`/api/memory/...`): list, search, edit, forget
- [ ] `nvx memory` CLI commands
- [ ] Memory eval set: extraction precision, recall@5 on 30 test questions

### Phase 3: Face (weeks 8–9), "It has a face"
**Goal:** A real desktop app in the tray that you open with a hotkey.
**Exit demo:** Press `Ctrl+Space` → Neurovex window → chat with streaming and tool cards → browse and edit memories.

- [ ] Install Rust (rustup) + MSVC Build Tools + WebView2 check
- [ ] Tauri 2 + Vite + React + TS + Tailwind scaffold, design tokens (see ARCHITECTURE §14)
- [ ] Core started as a sidecar by the shell; tray icon; single instance; global shortcut
- [ ] Status bar (idle / thinking / speaking), conversation view, confirmation dialog
- [ ] Memory browser v1: timeline, search, facts list with edit/forget
- [ ] Cost counter, settings page (API keys go to the keychain, model choices, budgets)

### Phase 4: Voice (weeks 10–12), "It listens and speaks"
**Goal:** Speak to Neurovex and hear it answer, with first audio in under 2.5 s.
**Exit demo:** Hold the push-to-talk key → "what's on my list today?" → spoken answer starts in under 2.5 s. Interrupt it mid-sentence and it stops.

- [ ] **Spike first:** measure faster-whisper `base.en`/`small.en` and Kokoro on your CPU, pick models
- [ ] Separate `neurovex-voice` process talking to core over WebSocket
- [ ] Audio capture (`sounddevice`), Silero VAD endpointing
- [ ] STT (faster-whisper int8) with timing
- [ ] TTS (Kokoro) with **sentence-chunked streaming**; ElevenLabs optional backend
- [ ] State machine IDLE → LISTENING → TRANSCRIBING → THINKING → SPEAKING, barge-in
- [ ] Push-to-talk hotkey, then wake word "Hey Neurovex" (openWakeWord custom model)
- [ ] Voice overlay in the UI (waveform, live transcript)
- [ ] Latency dashboard: p50/p95 for each stage

### Phase 5: Life & Tasks (weeks 13–16), "It's useful"
**Goal:** Neurovex manages your tasks and tracks your body's signals well enough to replace your current to-do app.
**Exit demo:** "I need to finish the report, call the dentist, and maybe learn Docker someday" → three structured tasks. "I have 40 minutes and I'm tired, what should I do?" → a sensible pick. "Slept 1am to 7:30, felt rough" → logged, with a pattern alert after a few days.

- [ ] Task Brain: models, store, smart filters, advisor (next task, inbox triage, weekly plan, procrastination detection), focus sessions / Pomodoro
- [ ] Life OS: sleep/exercise/energy/mood/meal logs, baseline model, pattern alerts, analyzer
- [ ] MCP bridge (external MCP servers become skills) + calendar connector, read-only first (Google/Outlook via MCP, or an ICS URL)
- [ ] UI pages: `/tasks` (kanban, focus timer), `/life` (sleep chart, energy heatmap, streaks)
- [ ] Skills for both modules registered and covered by the golden routing set

### Phase 6: Ambient (weeks 17–18), "It's alive"
**Goal:** Neurovex notices context and speaks up, rarely and at the right moment.
**Exit demo:** Code for 90 minutes → a gentle break suggestion. Come back after 40 minutes away → "welcome back" with what changed. It stays quiet during focus sessions and quiet hours.

- [ ] Scheduler (APScheduler, persisted jobs, default schedule, editable by skill)
- [ ] Observers: active window (Win32), idle time (`GetLastInputInfo`), clipboard (**off by default**)
- [ ] Privacy filters: app/title denylist, secret detection, private mode toggle, no verbatim clipboard storage
- [ ] Proactive engine: rules, cooldowns, quiet hours, focus-aware, daily interruption budget
- [ ] Notification channels: voice / toast / in-app, chosen by urgency and your state
- [ ] "What is Neurovex watching?" transparency panel

### Phase 7: Rituals (weeks 19–20), "It knows me"
**Goal:** The flagship daily experiences.
**Exit demo:** At 7:00 the morning brief plays (sleep vs baseline, energy prediction, top 3 tasks, calendar, one insight, one question). At 21:00 an interactive evening reflection runs. On Monday a weekly review is ready.

- [ ] Morning brief engine + prompt template
- [ ] Evening reflection (interactive, voice or text)
- [ ] Weekly review
- [ ] Insights store + `/api/intelligence/*`
- [ ] "Today" home screen in the UI

### Phase 8: Hands (weeks 21–24), "It acts"
**Goal:** Neurovex takes actions in the digital world, safely.
**Exit demo:** "Find three reviews of the Keychron Q1 and save a summary to my notes" → plan shown → you approve → browser agent runs (visible progress) → file saved → action logged.

- [ ] Agent framework: plan → show → confirm → execute → log, with risk levels and step-level confirmation
- [ ] Browser agent (Playwright, separate profile, untrusted-content handling)
- [ ] File agent (allow-listed directories only, confirm before overwrite or delete, trash not delete)
- [ ] Code agent (sandboxed subprocess, no network by default, time and memory limits)
- [ ] Multi-agent chains (browser → file → code) through the orchestrator, plus more MCP servers (email, GitHub…) with per-server permissions
- [ ] Prompt-injection defenses (see ARCHITECTURE §13)
- [ ] `/agents` UI: live steps, pending confirmations, history

### Phase 9: Harden & Ship (weeks 25–26), "It's trustworthy"
**Goal:** Install it like a real app. Encrypted at rest. Full control over your data.
**Exit demo:** Install from an `.msi`. It starts with Windows and asks for your passphrase or Windows Hello. The DB file is unreadable without it. Export everything as JSON. "Forget last Tuesday" works. Restore works from backup.

- [ ] SQLCipher database encryption (or the fallback path decided in the Phase 0 spike)
- [ ] Vault: passphrase → Argon2id → key, auto-lock after inactivity
- [ ] Privacy: retention policies, redaction, export-all, secure nuke
- [ ] Encrypted automatic backups (daily, keep 14)
- [ ] Packaging: core as a PyInstaller/Nuitka sidecar inside the Tauri bundle, autostart, auto-update
- [ ] Security review pass + dependency audit (`pip-audit`, `npm audit`)

---

## 7. Timeline & effort

| Assumption | Value |
|---|---|
| Hours per week | 10–15 (evenings + weekends) |
| Working with an AI coding assistant | Yes, using the prompts in PROMPTS.md |
| Total | **~26 weeks (~6 months)** to v1 |
| First genuinely useful version | **End of Phase 2 (week 7)**: a text assistant that remembers you |
| "Feels like JARVIS" | **End of Phase 7 (week 20)** |

The original estimate of 6–10 weeks would only hold if every library cooperates on Windows the first time. They won't. The extra time is mostly in Voice (latency tuning), Memory (extraction quality) and Agents (safety). Those are the parts that make or break the experience.

**If you want it faster:** cut Phase 8 to the browser agent only, and skip ElevenLabs and the wake word (push-to-talk is enough). That gets you to roughly 19 weeks.

---

## 8. How to build it (session workflow)

1. **One milestone per session.** Open [docs/PROMPTS.md](docs/PROMPTS.md), paste the **Context Header** with the "Current state" filled in, then paste the milestone prompt.
2. **The AI writes code and tests. You run them.** Don't merge anything with failing tests.
3. **Commit at every green test run.** Use one branch per milestone (`p2-memory`), and merge into `main` when the exit demo works.
4. **End every session with the Wrap-up prompt.** It updates `docs/PROGRESS.md` so the next session starts with accurate context.
5. **Stuck for more than 45 minutes?** Use the Unstuck prompt. Don't let the AI rewrite unrelated code.
6. **Use Neurovex yourself from Phase 1 onward.** Write down every annoyance in `docs/FRICTION.md`. That list is your real backlog.

---

## 9. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Scope creep / losing momentum | High | High | Phases end in a usable demo. Backlog lives in §10, not in the current phase. |
| Voice latency > 3 s on CPU | Medium | High | Cloud LLM for voice replies, sentence-chunked TTS streaming, latency spike before committing to models. |
| 15 GB RAM exhaustion | High | Medium | One small warm local model, lazy loading, RAM budget in §3, `nvx doctor` shows memory use. |
| Memory extraction pollutes facts | Medium | High | Confidence + provenance on every fact, user review queue, eval set, easy edit/forget. |
| Proactive features feel annoying | High | High | Cooldowns, quiet hours, interruption budget, "was this useful?" feedback that tunes the rules. |
| Prompt injection via web pages, files, clipboard | Medium | High | Untrusted-content tagging, side-effect tools always need confirmation, no secrets in context. |
| Cloud cost overrun | Low | Medium | Hard daily/monthly caps, Haiku default, prompt caching, cost dashboard. |
| Windows audio and driver quirks | Medium | Medium | `sounddevice`, device selection in settings, `nvx doctor` audio test. |
| Privacy leak (window titles, clipboard) | Medium | High | Observers off until you opt in, denylist, private mode, transparency panel, local-only processing for observer data. |
| Library churn (Tauri, Kokoro, openWakeWord) | Medium | Low | Pin versions, isolate each behind an interface, spikes before commitment. |

---

## 10. Backlog (after v1)

Ideas that are **not** in the v1 roadmap on purpose. Pull one in only when the roadmap is done or one of them is clearly more valuable than the current phase.

- **Learn Engine:** spaced-repetition from things you read and copy, "explain what I learned this week", learning-path tracking.
- **Work Agent:** email triage and drafting (through MCP), meeting prep from calendar + memory, project status reports.
- **Mobile companion:** a phone PWA or Telegram bot that talks to your home Neurovex over Tailscale.
- **Health integrations:** import sleep and steps from a wearable (Garmin, Fitbit, Health Connect exports).
- **Finance module:** spending categories from bank CSV exports, monthly review.
- **Screen understanding:** on-demand "what am I looking at?" with a screenshot sent to a vision model (opt-in, per request).
- **Local-first upgrade path:** with a GPU, flip the routing policy so most work stays local.
- **Personal fine-tuning:** LoRA on your own corrections so the local model learns your phrasing.
- **Multi-device sync:** encrypted sync of the memory DB between PCs.
- **Plugin marketplace:** third-party modules with signed manifests and the permission system.

---

## 11. Definition of "Neurovex v1 is done"

- [ ] Starts with Windows, lives in the tray, opens with a hotkey.
- [ ] Text and voice conversation, with first audio < 2.5 s at p50.
- [ ] Remembers facts and events across weeks. Every memory is visible, editable and deletable.
- [ ] Manages your tasks and life logs well enough that you stopped using another app for them.
- [ ] Morning brief and evening reflection run daily and are worth listening to.
- [ ] Can browse, read and write files, and run code on request, with confirmation and an audit log.
- [ ] Data encrypted at rest. Secrets in the keychain. Export and nuke work.
- [ ] Monthly cloud cost stays under the cap you set.
- [ ] You used it daily for 14 consecutive days.

---

## 12. Before you start (one-time setup)

```powershell
# 1. uv (Python project manager, also installs Python 3.12)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Ollama models (small chat model + embeddings). Check sizes with `ollama show`.
ollama pull nomic-embed-text
ollama pull qwen3:4b        # or the smallest qwen3.5 variant available; keep < ~3.5 GB

# 3. Anthropic API key → https://console.anthropic.com  (stored in the keychain in Phase 0)

# 4. Before Phase 3: Rust + MSVC build tools (needed by Tauri)
winget install Rustlang.Rustup
winget install Microsoft.VisualStudio.2022.BuildTools   # select "Desktop development with C++"

# 5. Before Phase 4: espeak-ng (Kokoro uses it as a fallback for unknown words)
winget install eSpeak-NG.eSpeak-NG

# 6. Git
git init
```
