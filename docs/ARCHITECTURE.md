# Neurovex Architecture

> How Neurovex is built. Read [PLAN.md](../PLAN.md) first for the why and the roadmap.

**Contents**
1. System overview
2. Processes
3. Inside the core
4. Life of a request
5. LLM layer
6. Skills & tools
7. Modules
8. Event bus & event catalog
9. Memory Nexus
10. Storage
11. Voice pipeline
12. Ambient awareness & proactive engine
13. Security & privacy model
14. Desktop shell (UI)
15. Repository layout
16. Testing, evals & observability

---

## 1. System overview

```
                         ┌──────────────────────────────────────────────┐
                         │            NEUROVEX SHELL (Tauri 2)          │
                         │  React UI · tray · hotkey · notifications    │
                         └───────────────┬──────────────────────────────┘
                                         │ REST + WebSocket (127.0.0.1, bearer token)
┌──────────────────────┐    WebSocket    ▼
│  NEUROVEX VOICE      │◄──────►┌─────────────────────────────────────────────────────────┐
│  (Python process)    │        │                 NEUROVEX CORE (Python)                  │
│  mic · VAD · STT     │        │                                                         │
│  TTS · speaker       │        │  Interfaces   REST · /ws/chat · /ws/events · CLI (nvx)  │
│  hotkey · wake word  │        │  ─────────────────────────────────────────────────────  │
└──────────────────────┘        │  Conversation Engine  fast path → agent loop → confirm  │
                                │  ─────────────────────────────────────────────────────  │
                                │  Skills & Modules   TaskBrain · LifeOS · Calendar ·     │
                                │                     Agents · MCP bridge · Built-ins     │
                                │  ─────────────────────────────────────────────────────  │
                                │  Memory Nexus   working · episodic · semantic · proc.   │
                                │  LLM Layer      router · providers · cost · budgets     │
                                │  Ambient        scheduler · observers · proactive       │
                                │  Security       keychain · permissions · audit · vault  │
                                │  ─────────────────────────────────────────────────────  │
                                │  Event Bus (in-process, persisted)  ·  SQLite + vec     │
                                └──────────┬──────────────────────────────┬───────────────┘
                                           │                              │
                                  ┌────────▼────────┐          ┌──────────▼──────────┐
                                  │ Ollama (local)  │          │ Anthropic API       │
                                  │ CPU, small model│          │ Haiku/Sonnet/Opus   │
                                  │ + embeddings    │          │ (cloud)             │
                                  └─────────────────┘          └─────────────────────┘
                                           Optional: MCP servers (calendar, email, ...)
```

---

## 2. Processes

| Process | Language | Responsibilities | Why it's separate |
|---|---|---|---|
| **neurovex-core** | Python 3.12, asyncio | API, conversation, memory, LLM routing, modules, scheduler, observers, proactive engine, agents | The single source of truth. Everything stateful lives here. |
| **neurovex-voice** | Python 3.12 | Microphone, VAD, wake word / push-to-talk, STT, TTS, audio playback | Audio and ML inference are CPU-heavy and use blocking I/O. A crash in an audio driver must not take down the brain. It can restart on its own. |
| **neurovex-shell** | Tauri 2 (Rust) + React | Window, tray, global hotkey, OS notifications, autostart. Launches core and voice as sidecars. | The native desktop experience. |
| Ollama | external | Local LLM + embeddings | Already a service on your machine. |

**Background "daemon" work (observers, scheduler, proactive engine) runs inside core as asyncio tasks.** Blocking calls (Win32 APIs, clipboard) run through `asyncio.to_thread`. The original plan used a separate daemon process talking over Redis. That doubles the state and the failure modes for no benefit on one machine.

**Inter-process communication:** the voice process and the shell are both *clients* of core's WebSocket API. Core never calls into them directly. It publishes events (`voice.speak`, `ui.notify`) that they subscribe to.

**Lifecycle:** In development, run each process from a terminal (`nvx serve`, `nvx voice`, `npm run tauri dev`). In production, the shell starts core, waits for `/health`, then starts voice. If core dies, the shell restarts it with backoff.

---

## 3. Inside the core

Layers depend only on the layers below them. Nothing imports "upward". This is what prevents circular imports.

```
interfaces/      api routes, websocket handlers, cli          ── depends on ↓
conversation/    engine, fast path, agent loop, confirmations ── depends on ↓
modules/         task_brain, life_os, calendar, agents, mcp   ── depends on ↓
intelligence/    morning brief, reflection, weekly review     ── depends on ↓
memory/ llm/ ambient/ security/                               ── depends on ↓
core/            config, logging, db, event bus, skills, module base, errors
```

Modules **never import each other.** They communicate through events and by calling skills through the registry. This keeps every module independently importable, testable and removable.

**Dependency wiring:** a single `Container` dataclass is built in the FastAPI lifespan. It holds `settings, db, bus, skills, llm, memory, permissions, audit`, and is passed to modules in `start(container)`. No global singletons, so tests build a container with fakes.

---

## 4. Life of a request

Example: you say *"Remind me to call the dentist tomorrow at 6, and what did I decide about the Docker course?"*

```
 1. voice      push-to-talk ▸ VAD endpoint ▸ faster-whisper ▸ "remind me to call…"
 2. voice→core WS message {type:"user_input", text, source:"voice", turn_id}
 3. engine     working memory += user turn; bus.publish("conversation.turn_started")
 4. fast path  slash command? no. Procedural trigger match ≥ 0.85? no. → agent loop
 5. context    MemoryNexus.recall(query, budget=1500 tokens):
                 semantic facts about "dentist", "Docker course"   (vector + graph)
                 episodes: last week's conversation about Docker   (vector × recency × importance)
                 working memory: last N turns (rolling summary beyond that)
 6. policy     LLM router: task=conversation, voice=true → claude-haiku-5-5, stream
 7. agent loop model returns tool calls:
                 task_brain.add_task(title="Call the dentist", due="2026-10-10T18:00")
                 memory.search_episodes(query="Docker course decision")
 8. permission add_task is risk=LOCAL_WRITE → auto-approved by policy, audited
 9. execute    skills run; results returned to the model as tool_result blocks
10. respond    model streams the answer ▸ engine splits into sentences ▸ core→voice "speak" chunks
11. voice      Kokoro synthesizes sentence 1 while the model is still writing sentence 2
12. post-turn  (background task, never blocks the reply)
                 MemoryNexus.update_from_turn(): extract facts/episodes (local model), embed, store
                 audit + cost records; bus.publish("conversation.turn_completed", timings)
```

**Fast path** (step 4) exists so common, deterministic things never pay for an LLM call:
- `/commands` typed in chat (`/focus 25`, `/log sleep 7.5`)
- Procedural workflow triggers (fuzzy match with `rapidfuzz` + embedding similarity, threshold 0.85)
- Exact confirmations ("yes", "go ahead", "cancel") when a confirmation is pending

---

## 5. LLM layer

### Providers
```python
class LLMProvider(Protocol):
    name: str
    tier: Literal["local", "cloud"]
    async def chat(self, messages, *, system, tools=None, max_tokens, temperature) -> LLMResponse
    def stream(self, messages, *, system, tools=None, ...) -> AsyncIterator[LLMEvent]   # text deltas, tool calls, usage
    async def embed(self, texts: list[str]) -> list[list[float]]                       # local only
    async def health(self) -> ProviderHealth
```
Implementations: `AnthropicProvider`, `OllamaProvider`, `MockProvider` (scripted responses for tests; also used for record/replay).

### Routing policy (config-driven, not hard-coded)

| Task type | Default model (this PC) | Why | With a GPU later |
|---|---|---|---|
| Embeddings | local `nomic-embed-text` | private, cheap, fast on CPU | same |
| Memory extraction / tagging | local small model → Haiku if JSON is invalid twice | private by default | local 8B |
| Observer summaries (window, clipboard) | **local only, never cloud** | privacy | same |
| Conversation (text) | `claude-haiku-5-5` | fast, cheap, good tool use | local 8–14B |
| Conversation (voice) | `claude-haiku-5-5` streaming | time-to-first-token | local 8B |
| Planning, weekly review, code, agents | `claude-sonnet-5-5` | reasoning quality | same |
| Explicit "think hard" | `claude-opus-5-5` | rare, expensive | same |
| Offline (no internet) | local small model | degraded but works | local |

The policy lives in `config/llm_policy.yaml`. Switching to "mostly local" after a hardware upgrade is a config edit, not a code change.

### Escalation (better than "hedging phrase" detection)
1. **Static:** the task type decides the tier (table above).
2. **Structural failures** escalate automatically: invalid JSON or tool arguments after one repair attempt, an empty answer, or a timeout.
3. **User signal:** "that's wrong" or a thumbs-down re-runs on the next tier and records the example.
4. **Learning:** a nightly job reviews which local task types failed most and suggests policy changes (shown to you, never applied silently).

### Budgets & cost
- Every call records `provider, model, module, task_type, input_tokens, output_tokens, cache_read_tokens, cost_usd, latency_ms`.
- Daily and monthly caps from config. At 80% you get a warning. At 100%, cloud calls raise `BudgetExceeded` and the router falls back to local with a spoken or visible note.
- Prices live in config (`llm_prices.yaml`) so they can be updated without code changes.
- **Prompt caching:** the system prompt is ordered stable → volatile (persona → tool definitions → user profile → retrieved memories → turn). The stable prefix gets a cache breakpoint.

### Persona (summary; full prompt in PROMPTS.md P1.2)
Neurovex is direct, warm without being saccharine, precise, a little futuristic in tone. It knows the user personally and refers to what it remembers naturally. It is honest about uncertainty and never invents memories. It keeps voice answers short (1–3 sentences unless asked for more). It doesn't open with "As an AI...". It states clearly when it is about to act.

---

## 6. Skills & tools

A **skill** is one capability, exposed to the LLM as a tool and callable directly by the fast path, workflows and other modules.

```python
class AddTaskParams(BaseModel):
    title: str
    due: datetime | None = None
    priority: Priority = Priority.MEDIUM
    estimated_minutes: int | None = None


@skill(
    id="task_brain.add_task",
    description="Create a task. Use when the user says they need to / should / must do something.",
    risk=Risk.LOCAL_WRITE,
    permissions=[Perm.TASKS_WRITE],
    examples=["remind me to call mom", "I need to finish the report by friday"],
)
async def add_task(ctx: SkillContext, p: AddTaskParams) -> SkillResult: ...
```

- **Parameters are Pydantic models.** JSON Schema is generated for the LLM, and arguments are validated before execution.
- **Risk levels** drive the confirmation gate:

| Risk | Examples | Default policy |
|---|---|---|
| `READ` | list tasks, search memory, get time | auto |
| `LOCAL_WRITE` | add task, log sleep, save note | auto + audit |
| `DESTRUCTIVE` | delete task, forget memory, overwrite file | confirm |
| `EXTERNAL` | submit form, send message, calendar write, purchase | **always confirm** |
| `EXEC` | run code, shell command | **always confirm** + sandbox |

- **Tool selection at scale:** with more than ~25 skills, don't send every tool on every turn. Always include core tools, then add the top-k skills by embedding similarity to the user's message, plus all tools of any module mentioned in working memory.
- **Results** are `SkillResult(ok, data, display, error)`. `display` is a hint for the UI to render a rich card (task card, chart, file preview).

### MCP bridge
Neurovex is an **MCP client**: configured MCP servers (Google Calendar, Gmail, filesystem, GitHub and so on) are listed in `config/mcp.yaml`. Each server's tools are registered as skills named `mcp.<server>.<tool>`, with a risk level you assign per server (default `EXTERNAL` → always confirm). Later, Neurovex can also *be* an MCP server, exposing your memory to other AI tools with your permission.

---

## 7. Modules

```python
class NeurovexModule(ABC):
    manifest: ModuleManifest  # id, name, version, permissions, subscriptions, settings schema

    async def start(self, c: Container) -> None: ...
    async def stop(self) -> None: ...
    async def handle_event(self, event: Event) -> None: ...
    async def health(self) -> ModuleHealth: ...
    def skills(self) -> list[Skill]: ...
    def schedules(self) -> list[ScheduleSpec]: ...  # default cron jobs this module contributes
    def context_providers(
        self,
    ) -> list[ContextProvider]: ...  # e.g. LifeOS adds "slept 5.5h" to context
```

```yaml
# neurovex/modules/life_os/manifest.yaml
id: life_os
name: Life OS
version: 0.1.0
permissions: [life.read, life.write, notify]
subscriptions: [ritual.morning_brief.requested, ritual.evening_reflection.requested, clock.midnight]
```

- The module manager discovers modules in `neurovex/modules/*/manifest.yaml`, checks permissions, and starts them in dependency order. A module that fails to start is marked `degraded`. It never crashes core.
- **Hot reload:** not built in. `uvicorn --reload` handles development. Real plugin hot-reload is backlog.
- **Context providers** let modules contribute short, relevant lines to the system prompt ("Current focus session: 'Report draft', 18 min left"), each with a token budget.

---

## 8. Event bus & event catalog

**Design:** an in-process async pub/sub with:
- an event envelope: `id (uuid7), type, source, ts, payload, correlation_id, turn_id`
- glob subscriptions (`task.*`, `*.failed`)
- **persistence:** every event is written to the `events` table before dispatch (outbox pattern), which gives replay and debugging
- handlers run concurrently with a per-handler timeout. On failure: retry 3× with backoff, then the event goes to `dead_letters` with the error
- **external fan-out:** `/ws/events` streams selected event types to the UI and voice process
- the same `EventBus` interface could be backed by Redis Streams later, if Neurovex ever runs across machines

**Naming:** `domain.entity.verb_past` in lowercase (`task.created`, `voice.state.changed`).

| Event | Producer | Consumers |
|---|---|---|
| `conversation.turn_started` / `turn_completed` | engine | memory, metrics, UI |
| `confirmation.requested` / `resolved` | engine, agents | UI, voice |
| `voice.state.changed` (idle/listening/transcribing/thinking/speaking) | voice | UI |
| `voice.speak` | engine, proactive | voice |
| `voice.wake_detected` | voice | engine, UI |
| `task.created` / `completed` / `deferred` | task_brain | memory, proactive |
| `focus.started` / `ended` | task_brain | proactive, voice, ambient |
| `life.logged` (sleep/exercise/energy/mood/meal) | life_os | memory, proactive |
| `ambient.app.changed`, `ambient.work_session.started` / `ended` / `long` | window observer | proactive, memory |
| `ambient.user.state_changed` (active/away/idle) | idle observer | proactive, voice |
| `ambient.clipboard.changed` (metadata only) | clipboard observer | memory |
| `clock.midnight`, `ritual.*.requested` | scheduler | modules, intelligence |
| `proactive.suggestion.made` / `feedback` | proactive | UI, metrics |
| `agent.plan.created`, `agent.step.started` / `completed` / `failed` | agents | UI, audit |
| `llm.call.completed`, `llm.budget.warning` | llm | cost tracker, UI |
| `memory.fact.asserted` / `retracted`, `memory.episode.recorded` | memory | UI |

---

## 9. Memory Nexus

### Four memory types

| Type | Holds | Storage | Lifetime |
|---|---|---|---|
| **Working** | Current conversation turns, active tool results | RAM (per session) + rolling summary | Session; summary goes to episodic |
| **Episodic** | Timestamped events: "on Tue you decided to drop the Docker course" | `episodes` table + vector index | Long (retention policy) |
| **Semantic** | Facts about you: preferences, goals, people, skills, beliefs | `entities`, `facts` tables (subject–predicate–object) + vector index | Until contradicted or deleted |
| **Procedural** | Workflows you taught: trigger → steps | `workflows` table | Until deleted |

### Semantic facts are temporal and sourced
```
facts(id, subject_id, predicate, object_id | object_value, confidence 0–1,
      source_episode_id, valid_from, valid_to NULL, created_at, updated_at, status: active|superseded|retracted|pending_review)
```
- "I moved to Berlin" sets `valid_to` on the old `lives_in` fact and inserts the new one. History is kept, not overwritten.
- Low-confidence extractions (< 0.6) go to `pending_review`. The UI shows a "Neurovex thinks it learned…" queue you can accept or reject.
- Every fact links to the episode it came from, so "why do you think that?" always has an answer.

### Write path (after every turn, in the background)
1. Working memory appends the turn. Beyond ~12 turns, older turns are folded into a rolling summary.
2. **Extraction** (local model, JSON schema output): `{episodes:[{summary, importance, tone, tags}], facts:[{s,p,o,confidence}], workflow_candidates:[...]}`
3. Facts are normalized against existing entities (embedding match + alias table) to avoid duplicates like "mom", "my mother" and "Mum".
4. Embeddings are computed (local) and written to `sqlite-vec`.
5. Events are published (`memory.*`).

### Read path (before every LLM call)
```
score(episode) = 0.55·similarity + 0.25·recency_decay(half-life 14d) + 0.20·importance
```
- Top episodes, plus facts about entities mentioned in the query (graph hop 1), plus the **user profile** (a compact, cached summary of core facts, regenerated nightly), plus working memory.
- Everything is assembled under a **token budget** (default: profile 300, facts 400, episodes 600, working memory 1500). Truncate by score, never randomly.
- Retrieved memories are wrapped in `<memory>` tags in the prompt with dates, so the model can say "on 3 Oct you mentioned…".

### Nightly consolidation (`clock.midnight` + 30 min)
- Summarize the day into one "day episode".
- Merge duplicate facts and entities. Decay the importance of old, never-recalled episodes.
- Regenerate the user profile summary.
- Apply retention policies.

### User control (non-negotiable)
- `/memory` UI and `nvx memory` CLI: browse, search, edit, forget.
- "Forget that" in conversation retracts the facts and episodes from the current turn.
- "What do you know about X?" always works and cites sources.

---

## 10. Storage

### Data directory (`%LOCALAPPDATA%\Neurovex\`)
```
Neurovex/
├── neurovex.db          # SQLite (WAL) — all structured data + sqlite-vec tables
├── vault.bin            # encrypted secrets beyond API keys (Phase 9)
├── logs/                # JSON logs, rotated daily, 14 days
├── models/              # whisper, kokoro, wake word models
├── browser-profile/     # isolated Playwright profile
├── sandbox/             # code agent scratch
├── backups/             # encrypted daily snapshots
└── exports/
```
API keys live in **Windows Credential Manager** (via `keyring`), never in `.env` in production.

### Tables (by owner)

| Owner | Tables |
|---|---|
| core | `events`, `dead_letters`, `settings_kv`, `schema_version` (Alembic) |
| security | `audit_log` (hash-chained), `permissions_granted` |
| llm | `llm_calls`, `routing_feedback` |
| memory | `episodes`, `episodes_vec`, `entities`, `entity_aliases`, `facts`, `facts_vec`, `workflows`, `conversations`, `messages` |
| task_brain | `projects`, `tasks`, `task_deps`, `task_events`, `focus_sessions` |
| life_os | `sleep_logs`, `exercise_logs`, `energy_logs`, `mood_logs`, `meal_logs`, `life_goals`, `life_baseline` |
| ambient | `app_sessions`, `user_state_spans`, `captured_resources`, `scheduled_jobs` (APScheduler) |
| proactive | `proactive_rules_state`, `suggestions` (with feedback) |
| agents | `agent_runs`, `agent_steps` |
| intelligence | `briefs`, `reflections`, `insights` |

- **One database, one connection pool, WAL mode**, `busy_timeout=5000`. Single-user load is tiny.
- Each module owns its tables and its Alembic migrations folder. No module reads another module's tables directly; it calls a skill or a read API.
- **Timestamps:** stored in UTC, converted to your local timezone at the edges.

---

## 11. Voice pipeline

### State machine
```
           hotkey/wake            VAD end-of-speech        transcript ready
  IDLE ─────────────► LISTENING ─────────────────► TRANSCRIBING ─────────────► THINKING
   ▲                     │ timeout/no speech                                       │ first sentence ready
   │                     ▼                                                         ▼
   └──────────────────── IDLE ◄──────── playback finished ◄──────────────────── SPEAKING
                                                                                   │ hotkey/wake (barge-in)
                                                                                   ▼
                                                                         stop audio → LISTENING
```

### Latency budget (target: first audio < 2.5 s at p50)

| Stage | Budget | Technique |
|---|---|---|
| End-of-speech detection | 300–500 ms | Silero VAD, 400 ms silence tail (tunable) |
| STT | 300–800 ms | faster-whisper `small.en` int8 on 12 cores; `base.en` if slower; warm model |
| Context + routing | 50–150 ms | cached user profile, vector search in-process |
| LLM first sentence | 500–900 ms | Haiku streaming, short voice-mode system prompt |
| TTS first chunk | 150–400 ms | Kokoro on CPU, synthesize per sentence as it streams |
| **Total to first audio** | **~1.3–2.75 s** | numbers are estimates: **measure in the Phase 4 spike** |

- **Sentence-chunked streaming:** split the LLM token stream at sentence boundaries (`.?!` plus a minimum length) and send each sentence to TTS immediately. The biggest perceived-latency win.
- **Filler** ("One moment…"): only if THINKING exceeds 1.5 s, using a pre-synthesized clip with zero delay. Not on every turn, or it gets irritating.
- **Barge-in:** the hotkey or wake word during SPEAKING stops playback, cancels the remaining TTS queue, and tells core to cancel the stream.
- **Echo:** while SPEAKING, the wake word detector stays on with higher sensitivity. Plain VAD-triggered listening is paused, so Neurovex doesn't hear itself.
- **Wake word:** "Hey Neurovex" (3+ syllables, distinctive, good for low false-accepts). openWakeWord custom model trained on synthetic samples, or Porcupine custom keyword (needs a free Picovoice AccessKey). **Push-to-talk ships first.**
- **Kokoro voice:** audition `bm_george`, `am_michael`, `af_heart` and `bf_emma` for a calm, intelligent tone. Voice and speed are set in settings.
- **Instrumentation:** each turn logs `t_wake, t_speech_end, t_transcript, t_first_token, t_first_sentence, t_first_audio, t_done`. Shown as p50/p95 in the UI.

---

## 12. Ambient awareness & proactive engine

### Observers (all opt-in, all local-only)

| Observer | Method (Windows) | Records | Never records |
|---|---|---|---|
| Active window | `GetForegroundWindow` + `GetWindowText` + process name (pywin32/psutil), polled every 5 s | app, category, title (after denylist filter), session spans | titles from denylisted apps (password managers, banking, private browsing) |
| Idle | `GetLastInputInfo` via ctypes, every 10 s | active / away (>2 min) / idle (>15 min) spans | — |
| Clipboard | `win32clipboard`, every 2 s, **off by default** | type, length, local-model topic, URL title | content verbatim; anything that looks like a secret (keys, tokens, passwords, card numbers) is dropped before any processing |
| Calendar | connector / MCP, every 10 min | upcoming events | — |

- App categorizer: rules first (`code.exe` → CODING), local LLM for unknowns, results cached.
- **Private mode** (tray toggle or "Neurovex, go private") pauses all observers and memory writes.
- **Transparency panel:** shows exactly what each observer captured in the last hour.

### Proactive engine
```python
@rule(on="ambient.work_session.long", cooldown=timedelta(minutes=45), priority=Priority.LOW)
async def suggest_break(evt, ctx) -> Suggestion | None:
    if ctx.focus_session_active or ctx.in_quiet_hours:
        return None
    return Suggestion(
        text="You've been coding for 90 minutes. Stretch for five?", channel=Channel.AUTO
    )
```
- **Gates, in order:** private mode → quiet hours → do-not-disturb (focus session, fullscreen app, in a meeting) → per-rule cooldown → **daily interruption budget** (default 6) → priority threshold.
- **Channels:** `voice` (only if you're active and not in a call), `toast`, `in_app`. `AUTO` picks based on urgency and your state.
- **Feedback loop:** every suggestion has 👍 / 👎 / "not now". Rules with a lot of 👎 get their cooldown increased automatically, and you're shown that this happened.

---

## 13. Security & privacy model

### Threat model

| Threat | Mitigation |
|---|---|
| Another local app or a **malicious website calling `localhost`** (CSRF, DNS rebinding) | Bind to `127.0.0.1` only. A random bearer token is generated at install and stored in the keychain; the shell and voice read it from there. Check the `Origin` header on WebSockets. Reject an unexpected `Host` header. |
| **Prompt injection** from web pages, files, emails, clipboard | Tool results from untrusted sources are wrapped in `<untrusted_content>` with an instruction that they are data. `EXTERNAL`/`EXEC`/`DESTRUCTIVE` tools **always** require human confirmation. Confirmation dialogs show the actual parameters. During an agent run, no tool can change permissions or settings. |
| Secrets leaking into prompts or logs | Secrets only in the keychain or vault. A log redaction processor in structlog. Secret-pattern scrubbing before any cloud call. |
| Stolen laptop / disk copy | BitLocker (OS) + SQLCipher DB encryption (Phase 9) + encrypted backups. |
| Tampering with history | Hash-chained audit log; `nvx audit verify`. |
| Runaway agent | Step and time limits per run, a kill switch (hotkey + tray), sandboxed code execution, file agent allow-list. |
| Runaway cost | Budget caps with hard stop. |
| Cloud provider data exposure | Minimal context, observer data never sent to the cloud, "what was sent" viewer per turn. |

### Components
- **Permissions:** declared in module manifests. Granted on first use through a UI prompt and stored in `permissions_granted`. Checked at the skill-execution boundary (one place, impossible to forget).
- **Audit log:** `audit_log(id, ts, actor, action, params_hash, outcome, confirmed_by_user, prev_hash, hash)` where `hash = sha256(prev_hash ‖ canonical_json(entry))`. Params are hashed, not stored in plaintext. Sensitive details live in the domain tables, which are encrypted.
- **Encryption at rest (Phase 9):** SQLCipher with a key derived by Argon2id from your passphrase, or a random key protected by Windows DPAPI / Windows Hello if you prefer no passphrase. The **Phase 0 spike** checks that `sqlite-vec` loads under SQLCipher. If it doesn't, the fallback is AES-GCM field-level encryption of content columns + BitLocker.
- **Vault:** auto-locks after 30 min of inactivity. Locked means cloud calls and memory reads pause until you unlock.
- **Privacy controls:** retention per memory type, redaction ("forget everything about X"), export-all (JSON + attachments as a zip), nuke (delete the DB, vectors, logs, backups and keychain entries, with a typed confirmation).

---

## 14. Desktop shell (UI)

**Stack:** Tauri 2 · Vite · React 19 · TypeScript · Tailwind · TanStack Query (REST) · Zustand (live state from WebSockets) · Motion (animations) · Recharts · d3-force (knowledge graph).
**Tauri plugins:** `global-shortcut`, `notification`, `autostart`, `shell` (sidecars), `single-instance`, `window-state`.

### Design language
| Token | Value |
|---|---|
| Background | `#0A0A0B` · surfaces `#111214` / `#17181B` · borders `#24262B` |
| Text | primary `#E6E8EB` · secondary `#9BA1A8` · muted `#5F656D` |
| Accent | `#7BA7BC` (desaturated blue-white) · accent-dim `#7BA7BC33` |
| Status | ok `#7FB89A` · warn `#C8A96A` · danger `#C27474` (desaturated) |
| Type | Inter (prose), JetBrains Mono (data, timestamps, IDs) |
| Shape | 0–2 px radius on data elements; 1 px borders; 8 px grid |
| Motion | 150–200 ms ease-out; no bounce; pulsing dot for "thinking" |

### Screens
| Route | Phase | Content |
|---|---|---|
| `/` Home / Today | 3 → 7 | Sidebar (status, time, quick actions), conversation center, right panel (top tasks, sleep, energy, focus time) |
| Voice overlay | 4 | 80% dark overlay, waveform, live transcript, streaming reply |
| `/memory` | 3 | Timeline, semantic search, facts table + graph, review queue, workflows |
| `/tasks` | 5 | Kanban, project switcher, focus timer, "what now?" |
| `/life` | 5 | Sleep (14 nights), energy heatmap, exercise streak, quick-log |
| `/agents` | 8 | Live steps, pending confirmations, history |
| `/settings` | 3 | Keys, models and policy, budgets, voice, observers, quiet hours, permissions, privacy |
| `/system` | 3 | Health, latency p50/p95, costs, logs, "what was sent" |

### WebSocket protocol (`/ws/chat`)
```jsonc
// client → core
{"type":"user_input","turn_id":"…","text":"…","source":"text|voice"}
{"type":"confirm","confirmation_id":"…","approved":true}
{"type":"cancel","turn_id":"…"}
// core → client
{"type":"turn_started","turn_id":"…"}
{"type":"token","turn_id":"…","text":"…"}
{"type":"tool_call","turn_id":"…","skill":"task_brain.add_task","params":{…}}
{"type":"tool_result","turn_id":"…","skill":"…","display":{"kind":"task_card",…}}
{"type":"confirmation_required","confirmation_id":"…","summary":"…","risk":"EXTERNAL","params":{…}}
{"type":"turn_completed","turn_id":"…","timings":{…},"cost_usd":0.0012}
{"type":"error","turn_id":"…","message":"…"}
```

---

## 15. Repository layout

```
Nootropic/                          # repo root (the folder name stays; the product is Neurovex)
├── PLAN.md
├── CLAUDE.md                       # brief for AI assistants (created in P0.1)
├── pyproject.toml                  # uv-managed; workspace for core + voice
├── uv.lock
├── Makefile                        # dev, test, lint, fmt, migrate, serve, voice, ui
├── .env.example
├── config/
│   ├── llm_policy.yaml             # task type → model routing
│   ├── llm_prices.yaml
│   ├── mcp.yaml                    # external MCP servers
│   └── schedules.yaml              # default cron schedule
├── docs/
│   ├── ARCHITECTURE.md · DECISIONS.md · PROMPTS.md
│   ├── PROGRESS.md                 # updated at the end of every session
│   └── FRICTION.md                 # your annoyances while using it = the real backlog
├── neurovex/                       # CORE package
│   ├── main.py                     # FastAPI app + lifespan
│   ├── cli.py                      # `nvx` (Typer)
│   ├── container.py                # dependency container
│   ├── core/                       # config, logging, db, errors, event_bus, skills, module_base, module_manager
│   ├── security/                   # secrets (keyring), permissions, audit, vault, encryption, privacy
│   ├── llm/                        # base, anthropic, ollama, mock, router, cost, prompts/
│   ├── memory/                     # models, working, episodic, semantic, procedural, extraction, retrieval, nexus, consolidation
│   ├── conversation/               # engine, fast_path, agent_loop, confirmations, persona
│   ├── ambient/                    # scheduler, observers/{window,idle,clipboard}, privacy_filter, proactive/
│   ├── intelligence/               # morning_brief, evening_reflection, weekly_review, insights
│   ├── modules/
│   │   ├── builtins/               # time, notes, system info
│   │   ├── task_brain/             # manifest.yaml, models, store, advisor, focus, module
│   │   ├── life_os/                # manifest.yaml, models, store, analyzer, module
│   │   ├── calendar/
│   │   ├── agents/                 # base, browser, file, code, orchestrator
│   │   └── mcp_bridge/
│   ├── api/
│   │   ├── auth.py                 # bearer token + origin checks
│   │   └── routes/                 # health, chat (ws), events (ws), memory, tasks, life, costs, intelligence, agents, voice, settings, system
│   └── migrations/                 # Alembic
├── voice/                          # VOICE process package (neurovex_voice)
│   └── neurovex_voice/             # main, client (ws to core), capture, vad, stt, tts/{kokoro,elevenlabs}, wake, hotkey, player, state, metrics
├── shell/                          # Tauri 2 app
│   ├── src-tauri/                  # Rust: main.rs, tauri.conf.json, capabilities/, sidecar config
│   └── src/                        # React: app/, routes/, components/, stores/, lib/ws.ts, lib/api.ts, styles/
├── evals/                          # golden sets: routing.yaml, memory_qa.yaml, extraction.yaml + runner
├── scripts/                        # spikes (sqlite-vec, stt bench, tts bench), dev helpers
└── tests/                          # mirrors neurovex/ and voice/; fixtures; MockProvider scripts
```

---

## 16. Testing, evals & observability

| Layer | What | Tooling |
|---|---|---|
| Unit | stores, bus, registry, policies, filters, rules | `pytest`, `pytest-asyncio`, in-memory SQLite |
| Integration | API + WS end-to-end with `MockProvider` | `httpx.AsyncClient`, `asgi-lifespan` |
| LLM contract | providers against recorded responses | `respx` (HTTP mocking), record/replay fixtures |
| Evals | routing accuracy, extraction precision, memory recall@5, brief quality (LLM-as-judge rubric) | `evals/` runner, `nvx eval <suite>`, results stored per run so regressions are visible |
| Voice | STT word error rate on 20 of your own recordings, latency benchmark | `scripts/bench_voice.py` |
| UI | components + WS client | Vitest + Testing Library |
| Live (opt-in) | real Anthropic/Ollama smoke tests | `pytest -m live` |

**Observability:** structlog JSON → `logs/`. Every turn has a `turn_id` that connects logs, LLM calls, tool calls, audit entries and voice timings. The `/system` page shows health, p50/p95 latencies, error counts and cost. `nvx doctor` checks everything on demand.

**Quality gates (CI via GitHub Actions or a local pre-push hook):** ruff, pyright, unit + integration tests, and evals must not drop more than 3 points from the last baseline.
