# Neurovex Decision Log

> Each entry records where Neurovex departs from the original "Mirror" prompt pack (or makes a choice the pack left open), why, and when to revisit it. Add a new entry whenever you make a decision you'd otherwise forget the reason for.

Format: **Context → Decision → Why → Revisit if**

---

### D-001 · Name and identifiers
- **Context:** The prompt pack was written for "Mirror".
- **Decision:** Product **Neurovex**. Python package `neurovex`, CLI `nvx`, voice package `neurovex_voice`, wake phrase "Hey Neurovex", data dir `%LOCALAPPDATA%\Neurovex`, module base class `NeurovexModule`.
- **Why:** Your project. "Neurovex" (3 syllables, unusual phonemes) also makes a better wake word than "Mirror", which is a common word and would cause false triggers.
- **Revisit if:** never.

### D-002 · Hardware-driven split brain (cloud converses, local protects)
- **Context:** The pack targets ~75% local inference with 8–14B models. This PC has no discrete GPU (integrated Radeon, 512 MB) and 15 GB RAM.
- **Decision:** Claude (Haiku 5.5 by default) handles conversation. The local Ollama model (3–4B class, CPU) handles embeddings, memory extraction, observer summaries, classification and offline fallback. Routing lives in `config/llm_policy.yaml`.
- **Why:** An 8B model on CPU gives roughly single-digit to low-teens tokens per second and slow prompt processing for long contexts. That's fine for background jobs, too slow for voice. Private observer data stays local regardless.
- **Revisit if:** you add a 12–16 GB NVIDIA GPU or 32 GB+ RAM. Flip the policy, no code changes.

### D-003 · One SQLite database instead of Postgres + Redis + ChromaDB + SQLite
- **Context:** The pack's docker-compose runs four data services for a single-user desktop app.
- **Decision:** One SQLite file in WAL mode, with `sqlite-vec` for vectors, accessed through SQLAlchemy 2.0 async + aiosqlite, with Alembic migrations. Docker is not required to run Neurovex.
- **Why:** Single user and low write volume. Four services cost ~2–3 GB of RAM we need for models, add startup ordering problems, and make backup and encryption four times harder. One file means one backup, one encryption key, and atomic transactions across memory and tasks.
- **Revisit if:** you go multi-device with a central server. Then Postgres + pgvector, behind the same repository interfaces.

### D-004 · In-process event bus with SQLite persistence instead of Redis Streams
- **Context:** The pack picked Redis Streams for persistence and replay.
- **Decision:** An asyncio pub/sub with glob subscriptions. Every event is written to an `events` table before dispatch, with retries and a `dead_letters` table. Fan-out to other processes happens over WebSocket.
- **Why:** Same guarantees (persistence, replay, dead-letter queue) without a server. Redis on Windows needs Docker or WSL.
- **Revisit if:** components move onto separate machines. Implement `RedisStreamsBus` behind the same `EventBus` protocol.

### D-005 · Tool-calling agent loop instead of intent-enum classification
- **Context:** The pack's IntentRouter classifies input into 8 intent types, extracts entities, then searches skills.
- **Decision:** A **fast path** (slash commands, workflow triggers, pending confirmations) followed by an **agent loop** where the LLM sees skills as tools and chooses them directly. Large tool sets are narrowed by embedding similarity (top-k).
- **Why:** Current models are very good at tool selection and argument extraction in one step. A separate classifier adds latency, a second failure point, and an enum that never fits real requests ("remind me X and what did I decide about Y" is two intents). The golden routing set still measures accuracy.
- **Revisit if:** tool selection accuracy drops below ~90% on evals as skills grow. Then add a hierarchical step (pick module → pick skill).

### D-006 · Escalation by structure and feedback, not hedging-phrase scoring
- **Context:** The pack escalates local → cloud when a "quality score" based on hedging phrases is below 0.6.
- **Decision:** Static routing by task type. Automatic escalation on structural failures (invalid JSON or tool args, empty output, timeout). Escalation on user correction. A nightly review that *suggests* policy changes.
- **Why:** Hedging detection is noisy. Confident wrong answers pass it, and careful correct answers fail it. Structural failures are objective.
- **Revisit if:** you build an eval-backed quality classifier with real data.

### D-007 · faster-whisper + Silero VAD instead of openai-whisper + webrtcvad
- **Decision:** `faster-whisper` (CTranslate2, int8, CPU) with `small.en` (or `base.en` if latency demands), and Silero VAD for endpointing.
- **Why:** faster-whisper is about 4× faster than openai-whisper at the same accuracy and uses less RAM. `webrtcvad` needs a C compiler on Windows, and Silero is more accurate.
- **Revisit if:** you get a GPU (move to `large-v3-turbo` on CUDA), or you need multilingual input (drop `.en`).

### D-008 · Push-to-talk first, wake word second
- **Context:** The pack starts with Porcupine "Hey Mirror".
- **Decision:** Ship a global push-to-talk hotkey in Phase 4. Then add "Hey Neurovex" with **openWakeWord** (open source, trainable custom model) or **Porcupine** (custom keyword from the Picovoice console, needs a free AccessKey).
- **Why:** A custom wake word needs training, tuning for false accepts, and echo handling. Push-to-talk gets you real voice usage a week or two earlier, and it stays useful in noisy rooms and meetings.
- **Revisit if:** — (both stay; the wake word is additive).

### D-009 · sounddevice instead of pyaudio
- **Why:** `pyaudio` needs PortAudio build steps on many Windows Python versions. `sounddevice` ships wheels with PortAudio bundled and has a cleaner numpy API.

### D-010 · Voice is its own process; the daemon is not
- **Context:** The pack runs a separate daemon process (observers, scheduler, proactive engine, voice) talking to FastAPI through Redis.
- **Decision:** Observers, scheduler and proactive engine run as asyncio tasks inside core. Voice runs as a separate process and is a WebSocket client of core.
- **Why:** Observers are light polling loops, and keeping them next to memory and the bus avoids duplicated state. Voice holds heavy models, blocking audio I/O and native drivers, which are the most likely things to crash or hog the CPU. Isolating voice protects the brain.
- **Revisit if:** observer polling ever measurably affects API latency (unlikely).

### D-011 · Vite + React inside Tauri 2 instead of Next.js 14
- **Decision:** Tauri 2 + Vite + React 19 + TypeScript + Tailwind, plus TanStack Query, Zustand, Motion, Recharts and d3-force.
- **Why:** Tauri serves a static bundle. Next.js server features (SSR, server actions, API routes) can't run there and would need `output: 'export'`, which leaves Next.js as dead weight. Next.js 14 is also several majors behind. The UI can still be opened in a browser during development through the Vite dev server.
- **Revisit if:** you want a hosted web version that's separate from the desktop app. Then Next.js on Vercel as its own frontend talking to a tunnel or the API.

### D-012 · Security from Phase 0, not sprint 9
- **Context:** The pack says "security must be built in from the start" but schedules it last.
- **Decision:** Phase 0 ships keychain secrets, permission checks at the skill boundary, the hash-chained audit log, a localhost bearer token and Origin checks. Phase 9 adds DB encryption, the vault UI, retention and export/nuke.
- **Why:** Retrofitting permission checks into 40 skills and adding auth to an API that every client already uses unauthenticated is painful and error-prone.

### D-013 · SQLCipher instead of hand-rolled AES-GCM on database files
- **Context:** The pack encrypts "all SQLite databases" with AES-256-GCM directly.
- **Decision:** SQLCipher full-database encryption (key from Argon2id, or DPAPI / Windows Hello). AES-GCM from `cryptography` is used only for the vault file, backups and exports. A **Phase 0 spike** confirms SQLCipher + `sqlite-vec` work together on Windows. If they don't, the fallback is field-level AES-GCM on content columns + BitLocker.
- **Why:** Encrypting a live SQLite file yourself means decrypting it to disk to use it, which defeats the purpose, or writing a VFS. SQLCipher is the proven approach.

### D-014 · uv instead of Poetry
- **Why:** Much faster, manages Python versions itself (installs 3.12 without touching your system 3.11.9), handles workspaces (core + voice) natively, and is the current standard.

### D-015 · APScheduler instead of a custom cron scheduler
- **Decision:** APScheduler 3.x `AsyncIOScheduler` with a SQLAlchemy job store in the same database. Default jobs come from `config/schedules.yaml` and module `schedules()`.
- **Why:** Persistence, misfire handling, cron syntax and timezone handling are already solved. Don't rebuild them.

### D-016 · MCP for integrations
- **Decision:** Neurovex is an MCP client. External MCP servers (calendar, email, GitHub, filesystem…) become skills named `mcp.<server>.<tool>` with per-server risk levels. Later, Neurovex can expose its own memory as an MCP server.
- **Why:** Writing and maintaining Google Calendar, Gmail and Outlook integrations yourself is weeks of OAuth plumbing. MCP gives access to an existing ecosystem.

### D-017 · Clipboard observer off by default, never stores content
- **Decision:** Opt-in. Secret-pattern detection drops content before any processing. Only metadata and a local-model topic are stored. Observer data is never sent to cloud models.
- **Why:** The clipboard regularly holds passwords, 2FA codes and API keys. The pack's design sent clipboard content to an LLM for summarization. That's acceptable only locally, and only after filtering.

### D-018 · Localhost API is authenticated
- **Decision:** Bind `127.0.0.1`. Use a per-install bearer token stored in the keychain. Allow-list `Origin` on WebSocket upgrades. Validate the `Host` header.
- **Why:** Any website you visit can send requests to `http://localhost:PORT` (DNS rebinding, CSRF). An unauthenticated local API holding your entire life plus agent "hands" would be a serious hole.

### D-019 · No module hot-reload in v1
- **Context:** The pack asks for file-watching module hot-reload.
- **Decision:** Use `uvicorn --reload` in development. No runtime module hot-reload.
- **Why:** Safely reloading modules that hold DB connections, subscriptions, scheduled jobs and in-flight tasks is complex, and it buys little for a single developer.
- **Revisit if:** third-party plugins become a thing (backlog).

### D-020 · Honest timeline: ~26 weeks
- **Context:** The pack estimates 6–10 weeks.
- **Decision:** Plan for ~26 weeks at 10–15 h/week, with usable milestones at weeks 4, 7, 9, 12 and 16.
- **Why:** Voice latency tuning, memory extraction quality and agent safety each take real iteration. Planning for the realistic number avoids burnout when the optimistic one slips.

### D-021 · Build the News module first, from scratch
- **Context:** You asked for a news updater like [Aetherix](https://github.com/Rakibul-Islam-Nahim/Aetherix) before the Phase 0 roadmap. Aetherix uses Postgres + Docker on a VPS, Groq/Puku for summaries, and a Flutter app, and its LICENSE is "All rights reserved".
- **Decision:** Re-implement the idea (feeds → dedupe → LLM brief → ranked feed) as Neurovex's first module, written from scratch on the Neurovex stack: SQLite, Claude Haiku 5.5 / local Ollama, FastAPI, a single-file web UI, and the `nvx` CLI. No Aetherix code is copied. Only the minimum of Phase 0 was built (config, logging, DB, localhost guard).
- **Why:** You get a feature you can use today, and it lands in the architecture it will live in long-term instead of a separate stack to migrate later. Writing it fresh also respects the source repo's license.
- **Revisit if:** you want Aetherix's mobile app. Then expose the news API through the Phase 10 mobile companion rather than a separate server.

### D-022 · Seven free cloud AI providers replace local Ollama (supersedes part of D-002)
- **Context:** You want Neurovex to run on a production server, where local CPU inference (Ollama) isn't an option, and you want to use the free tiers of seven providers: Google Gemini, Groq, OpenRouter, Cerebras, Mistral AI, Cloudflare Workers AI and Hugging Face Inference Providers.
- **Decision:** A shared LLM layer (`neurovex/llm`). All seven are first-class providers behind one router:
  - **Provider order:** set with `NEUROVEX_LLM_PROVIDERS`. Default: Cerebras → Groq → Gemini → Mistral → OpenRouter → Cloudflare → Hugging Face.
  - **Failover:** rate limits, quota, outages, bad keys and invalid JSON each move the request to the next provider, with a matching cooldown.
  - **Load spreading:** a per-provider concurrency limit overflows busy moments to the next provider. Round-robin is optional.
  - **Usage log:** every attempt is recorded in `llm_calls`.
  - **One client for six APIs:** the OpenAI-compatible APIs share a single httpx client. It steps down from `json_schema` to `json_object` to a prompt-only request when a model rejects a JSON mode, and remembers what worked.
  - **Optional extras:** Claude (paid) and Ollama (local dev) remain providers, used only if added to the list.
  - **Secrets:** environment variables, then `.env`, then the OS keychain. Servers set `NEUROVEX_USE_KEYRING=false`.
- **Why:**
  - Free tiers are small and change often, so no single provider is reliable enough alone. Chaining seven turns several small quotas into one dependable service.
  - Six of the seven speak the OpenAI chat-completions dialect, so one well-tested client beats seven SDKs.
  - A provider-agnostic `complete_json(request, parse)` keeps modules independent of whoever is serving today.
- **Defaults verified 2026-10-10** against each provider's docs and live endpoints (all seven reachable; bad keys classified as auth errors). Model names change often: check with `nvx llm models <id>`, override with `NEUROVEX_<ID>_MODEL`.
- **Revisit if:** a provider drops its free tier or OpenAI compatibility, or you start paying for one. Then raise its concurrency and move it to the front of the list.

### D-023 · Default AI chain is Groq → Puku (narrows D-022)
- **Context:** You asked to keep it simple: use Puku and Groq, no need for so many providers.
- **Decision:** `NEUROVEX_LLM_PROVIDERS` defaults to `groq,puku`. Setup, status, doctor and the web panel show only the chain. The other providers' code stays in the catalog, off by default, so the integrations you asked for earlier aren't thrown away.
- **Why:** Fewer keys to manage. Groq's free tier is fast and reliable, and Puku is the fallback when its plan has quota.
- **Consequence:** with one free provider, capacity is Groq's limits (~8K tokens/min, ~200K tokens/day for `gpt-oss-120b`). Proactive pacing (`llm/ratelimit.py`) keeps Neurovex under them, and background news leaves ~2.5K tokens/min of headroom for chat.
- **Revisit if:** news or chat regularly wait on Groq. Then add one more key (Cerebras has the largest free daily budget) by listing it in `NEUROVEX_LLM_PROVIDERS`.

### D-024 · Chat uses a provider-neutral JSON step protocol, not native tool calling
- **Context:** Ask Neurovex needs tools (search news, follow topics...). Groq, Puku and others expose tool calling differently, or not at all.
- **Decision:** Each step, the model returns one JSON object through `LLMRouter.complete_json`: `{"action": "use_tool" | "respond", "tool", "arguments_json", "message"}`. Arguments are a JSON *string* so the schema stays valid under strict structured output. At most 4 tool steps per request. Tool results go back fenced as `<tool_result>` data.
- **Why:** One code path that works on every provider in the chain, with the same failover, pacing and usage log as everything else.
- **Trade-off:** no token streaming yet. Each step is one round trip of about 1–2 s on Groq.
- **Revisit if:** the chain settles on providers with solid native tool calling and streaming. Then switch the engine to native tool calls behind the same `SkillRegistry`.

### D-025 · Memory that costs (almost) nothing: piggyback writes, full-text recall
- **Context:** Phase 2 (Memory), with your requirement "super efficient". The only working AI is Groq's free tier (~8K tokens/min, ~200K/day), so the plan's original design (an extraction LLM call after every turn, embeddings, nightly consolidation) would eat the budget.
- **Decision:**
  - **Write:** every chat step can carry `memory` operations (add / update / forget by id). The model writes memory while it answers. **Zero extra calls**, roughly 20–150 output tokens when something is remembered.
  - **Read:** SQLite FTS5 (BM25, porter stemming) over memories. Each turn gets a `<memory>` block of instructions, preferences, pinned and importance-3 memories, plus keyword matches for the message. ~14 ms, no tokens to compute, capped at ~1,800 characters (~500 tokens); typically ~50–150 tokens.
  - **Episodic recall:** FTS5 over past chat messages (`memory.search`). There is no summarizing pass.
  - **Quality rules:** one fact per memory (a worked example in the prompt), updates keep whatever is still true, relative dates become absolute, near-duplicates merge locally (word overlap ≥ 0.75), and secrets are refused before storage.
  - **Your control:** Memory tab and `nvx memory`: view, search, add, edit, pin, forget, forget everything.
- **Why:** Memory becomes nearly free and fast, works on any provider, and stays explainable. You can see and edit every fact.
- **Trade-off:** keyword search misses synonyms ("job" vs "works as"). Core memories and the model's own `memory.search` cover most of it.
- **Revisit if:** memories grow into the hundreds and recall misses things. Then add embeddings (a free embedding API or a small local model), keeping FTS as the first pass.

### D-026 · Puku is the only default provider, through `/v1/messages` (replaces D-023's chain)
- **Context:** You asked to use just the Puku key. Puku's `/v1/chat/completions` still fails (HTTP 500 wrapping "Missing Authentication header"), for both models and with the other auth headers tried, while `/v1/messages` with the same key answers normally (checked 2026-11-04).
- **Decision:** `NEUROVEX_LLM_PROVIDERS` defaults to `puku`. Puku's catalog entry is `kind="messages"`, served by `MessagesProvider`: the Anthropic request format over plain HTTP, reusing the OpenAI client's auth, error classification and JSON extraction. Groq and the others stay in the catalog, off.
- **Why:** One key to manage, and it works today. Live: a burst of 6 chat messages all succeeded (2.6–7.7 s each); the same kind of burst ran out of budget on Groq's free tier.
- **Trade-off:** no fallback. If Puku is down or out of quota, chat and summaries pause until it recovers. Puku sends no rate-limit headers and its quota isn't visible to Neurovex, so there is no proactive pacing: a limit is only learned from an error, then the provider rests (1 hour for a plan quota).
- **Revisit if:** quota runs out or Puku proves unreliable. Listing another id in `NEUROVEX_LLM_PROVIDERS` restores failover with no code change; `groq,puku` would need the Groq key that is still in the keychain.

### D-027 · Tasks: deterministic by default, AI only for conversation
- **Context:** Phase 5 "Task Brain", in keeping with "super efficient". The plan's advisor assumed LLM calls.
- **Decision:** Quick-add and date parsing (`dates.py`) and the "what now" advisor (`planner.py`) are plain code. All-day tasks are stored as the end of their local day, so overdue is always `due_at < now`. Timed tasks remind when due. Reminders that come due with no page open wait for one, so they arrive late rather than get lost. In chat, deleting isn't offered: `tasks.cancel` is reversible and needs no confirmation flow.
- **Why:** Instant, free, works with no AI at all, and every suggestion can be explained and tested.
- **Trade-off:** the parser only knows fixed phrasing, and is deliberately conservative: "Study for SAT" stays a title, while "next friday" means the coming Friday. Reminders need an open page until the desktop shell (Phase 3). Without `NEUROVEX_TIMEZONE`, far-future dates use today's UTC offset, which can be an hour off across a daylight-saving change.
- **Revisit if:** quick-add often misreads you, or reminders get missed because no page is open.

### D-028 · Keeping chat cheap per request
- **Context:** Live testing on Groq showed about 3 chat steps a minute: it charges prompt + full `max_tokens` up front, so one step cost ~3.2K of 8K tokens/min. These savings also shorten requests on Puku, whose quota is unknown.
- **Decision:**
  - A step that only changes things carries its final reply, so it takes one model call instead of two (read tools always see their results first).
  - `then` runs several calls in one step.
  - Full signatures appear only for tool groups the request needs (hint words, plus the previous answer's groups); the rest are listed by id and still callable.
  - Chat sends no JSON schema (`self_described`): the prompt shows the format and the parser validates.
  - `max_tokens` is sized to measured output: chat 500, summaries 1,000, briefings 3,000.
  - Older answers are trimmed to 600 characters in history.
  - Failed attempts release their rate reservation, and one bad output gets a retry on the same provider.
- **Trade-off:** a very long answer can hit the 500-token cap and fail. An early reply is written before the result is seen, so the task card, not the sentence, is the source of truth.
- **Revisit if:** answers get cut off. Raise `STEP_MAX_TOKENS` first.

### D-029 · Today is composed locally; opening it never costs AI quota
- **Context:** Phase 7's plan had a morning brief written by an LLM from a prompt template. With Puku as the only provider and its quota invisible (D-026), a screen you open many times a day can't spend requests.
- **Decision:** `neurovex/today/` assembles the day from data Neurovex already has: tasks and the advisor, ranked stories, the latest AI briefing if under 24 hours old, and goals from memory. The summary sentence is built by plain code. The briefing stays on the news pipeline's own schedule. The home tab is Today; the remembered tab moved to a new storage key, so everyone starts there once.
- **Why:** Instant, free, works with no AI at all, and it gives a reason to open Neurovex daily (the plan's success test).
- **Trade-off:** the summary is factual rather than conversational. "Brief me" in chat gives the conversational version, at normal chat cost. The announcement only reaches open pages, and is skipped if more than 6 hours late, because Today is there anyway when you open it.
- **Revisit if:** the plain summary feels flat in daily use. Then add an optional AI-written morning note, at most once a day.

### D-030 · X accounts come through x-tweet-fetcher and FxTwitter, free
- **Context:** You asked for X in the news, with a firm rule: no payment. X's own API has no free read access (about $0.005 per post, checked 2026-11-04), and you pointed to `ythx-101/x-tweet-fetcher`.
- **Options tested live, 2026-11-04, anonymously:** FxTwitter's documented `/2/profile/{handle}/statuses` returned recent posts. Nitter instances were gone, blocked or returned no feed; X's embed endpoint answered "rate limit exceeded"; the public RSSHub route returned 404. x-tweet-fetcher (`xtf`, MIT, reviewed: no third-party dependencies, no shell calls) reads single posts and profiles with no setup, but its timelines failed here because they need a Nitter server or its browser backend.
- **Decision:** `news/xsource.py` uses `xtf` as a dependency pinned to the reviewed commit, as you asked, for what it can do free: reading X Articles in full (timelines give them no text; 0 → 4,499 characters on a real post), and timelines through your own Nitter if you set `NEUROVEX_X_NITTER_URL`. FxTwitter's timeline finds new posts otherwise. Public accounts only, the account's own posts (no replies or reposts), at most 5 new posts per account every 30 minutes, one account at a time. No key, no login.
- **Ruled out:** `xtf`'s browser backend, which poses as an ordinary browser to get past public Nitter servers' bot blocks; and logged-in scraping, which breaks X's terms and puts an X account at risk. Self-hosted Nitter is left for you to choose: it needs a sessions file, reported to mean real X account tokens, and the project has reportedly received a cease-and-desist from X.
- **Trade-off:** it depends on a free third-party service with no uptime promise, and X could block it. A failure shows on that source's row and retries on the next refresh; other news carries on. Threads lose everything after the first post, since continuations are replies. If more new posts arrive than the cap in one check, the older ones are skipped. Each new post costs one Puku summary.
- **Revisit if:** FxTwitter stops working. Then self-hosting FxEmbed (MIT) is the next free option to evaluate. The Nitter path is tested with fakes only, since no Nitter server was available.

### D-031 · Reach Neurovex through Tailscale from this PC; a free cloud server later, maybe
- **Context:** You asked how to host it, with no payment. It needs to stay on (refresh loops), keep a database on disk, and has no login yet.
- **Options checked 2026-11-06:** Render's free tier sleeps after 15 minutes and loses local files on restart. Fly.io has no free tier, and Koyeb lists no free compute. PythonAnywhere's free plan has no always-on tasks and only reaches approved sites. Vercel's functions stop after 5 minutes. Hugging Face's free disk doesn't persist. Google's e2-micro may charge for its public IP (unconfirmed). Oracle Always Free fits (2 OCPU, 12 GB, 200 GB, 10 TB out), but it halved that allowance without notice in June 2026, can reclaim servers that look idle (CPU, network and memory all under 20% for a week, and Neurovex uses under 1% CPU), and reportedly needs a card to sign up.
- **Decision:** keep it on this PC: auto-start at sign-in (`nvx autostart`), daily verified backups (`core/backup.py`), and access from your own devices through Tailscale Serve (free for personal use, HTTPS, reachable only by your devices). `NEUROVEX_ALLOWED_HOSTS` lets the page open at the Tailscale name without turning off the Host check.
- **Trade-off:** it's only reachable while the PC is on and awake. Future observers (active window, clipboard) need this PC anyway.
- **Revisit if:** the PC can't stay on. Then Oracle Always Free, reached through Tailscale with no public ports, after testing that X and the feeds answer from a cloud address. Token auth (P0.3) comes before any public address.

### D-032 · Sign-in: session cookies from one-time codes, with no exemption for local requests
- **Context:** with phone access through Tailscale, anyone who could reach the page could use it. P0.3 asks for a bearer token on every route.
- **Decision:** scripts send the API token (`Authorization: Bearer`, kept in the keychain). Browsers can't keep a token safely in page code, so they sign in once with a one-time code (`nvx signin` on this PC, or a code from Settings → Devices typed on a phone) and get an HttpOnly, SameSite=Strict session cookie, stored server-side only as a hash. Devices are listed and can be signed out one by one; idle ones expire after 90 days. 10 wrong tries in 10 minutes pause sign-in. The `X-Neurovex-Client` header stays required on writes, so a cookie alone can't be used from another site.
- **Why no exemption for this PC:** Tailscale Serve forwards your phone's requests from `127.0.0.1`, so "it came from this computer" would let every remote device in. This PC signs in once, like any device.
- **Not built yet:** P0.3's permission store (grants per module) needs the module manifests from P0.2. Risk levels and confirmation already guard destructive, external and exec skills at the one place skills run.

### D-033 · Module system: Python manifests and a manager now; event bus and Alembic when something needs them
- **Context:** P0.2 asks for manifests, a module manager, an event bus and a container, and P0.1 for Alembic. PLAN principle 6: add infrastructure only when a real limit is hit.
- **Built:** a `ModuleManifest` per module (Python constant: typed, no YAML dependency) and a `ModuleManager` that starts modules in dependency order. Before, any module failing at startup stopped the whole app; now it's `degraded`, and so are the modules depending on it, which `/health` and `/api/system/modules` show. Permissions per module (`tasks.read`, `tasks.write`, …), set in Settings → What chat may do, checked where every skill runs and where memory reaches chat. `/api/skills` lists every capability with what it needs.
- **Deferred:** the persisted event bus (nothing consumes domain events yet; per-module live streams already serve the pages) and Alembic (`create_all` plus automatic nullable columns still covers every schema change made so far). Routines or the proactive engine are the first real consumers of the bus; the first non-nullable column change brings Alembic.
- **Trade-off:** modules are listed in code, so a third-party module would need an edit to `main.py`. That's fine while every module ships with Neurovex.

### D-034 · Routines: taught through chat, run on a fast path with no AI
- **Context:** PLAN Phase 2 asks for routines you teach ("when I say 'start my day', do X then Y"); PROMPTS P2.1 sketches a procedural store with fuzzy triggers, templated params and a fast-path hook.
- **Decision:** `neurovex/routines/` with a `routines` table (name, trigger phrases, steps as tool + arguments). Chat teaches one through `routines.save`, which checks every step against the real skills before saving. Saying a trigger runs the steps through `SkillRegistry.execute` (so permissions and the audit log apply) before any AI is asked: zero AI calls, measured on Puku (teaching took 2, running 0). A routine stops at the first step that fails or needs confirmation.
- **Matching is strict on purpose:** the whole message equals a trigger, is a typo away (similarity ≥ 0.88), or starts with one (the rest fills `{{input}}`). "How do I start my day better?" runs nothing. Python's `difflib` does the fuzzy part, so there's no new dependency (P2.1 suggested rapidfuzz).
- **Placement:** its own package rather than `memory/procedural.py`, because routines run other modules' skills, like `conversation` and `today` combine modules.
- **Not yet:** scheduled routines ("every weekday at 8"). They need somewhere to deliver results when no page is open, which is where the event bus and the Phase 6 scheduler come in.
