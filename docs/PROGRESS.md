# Neurovex Progress

## Current state
- First module shipped: **News**, now personal (inspired by the Aetherix project, re-implemented from scratch on the Neurovex architecture).
- Runs as `uv run nvx serve` (FastAPI + web UI at http://127.0.0.1:8765) with a 10-min background refresh, live updates over SSE, plus a full `nvx news …` CLI.
- Personal layer:
  - "For you" ranking from followed/muted topics, categories and sources, plus affinity learned from opens, saves, more/less and hides (30-day fade).
  - Personal briefings written by the LLM.
  - Breaking-news alerts.
  - "New since your last visit".
  - Your interests are summarized first.
- AI: Puku only, through its `/v1/messages` endpoint (D-026). Other providers stay in the catalog, off.
- A partial Phase 0 exists (config, logging, SQLite, localhost guard, skill registry). Still missing from P0.1–P0.3: Alembic, event bus, module manager, permissions, audit log, token auth.
- **Ask Neurovex** chat (web Ask tab + `nvx chat`) answers with skills over the news module.
- **Memory** (Phase 2, first version): remembers you across conversations at no extra AI cost (D-025).
- **Today** (Phase 7, first version): the home screen, composed locally with no AI calls (D-029).
- **Tasks** (Phase 5, first version): to-dos, plain-words quick-add, "what should I do now" advisor, repeats and reminders, in the web UI, `nvx tasks` and chat (D-027).
- X accounts can be followed as news sources, free (D-030).
- The web UI is organized into six sections with a dark theme by default.
- 235 tests passing, ruff clean.

## Done
- 2026-10-09: Master plan, architecture, decisions and build prompts (PLAN.md, docs/).
- 2026-10-09: News module.
  - Fetches 7 default feeds with conditional GET.
  - Dedupes on canonical URL plus title fingerprint across sources.
  - Fetches full page text when a feed excerpt is thin.
  - Summarizes into summary / what happened / why it matters / key points / categories / importance 1–10, and marks off-topic articles as SKIPPED.
  - Retries failures up to 3×. Prunes after 30 days (bookmarks kept).
  - REST API, web UI (filters, search, bookmarks, read state, top stories, source management), CLI.
  - Live run: 7/7 feeds OK, 114 articles, local summaries verified.
- 2026-10-09: News personal layer.
  - Ranking:
    - `personalize.py` scores importance (40%), personal interest (40%) and freshness (20%), with plain-language reasons.
    - Muted topics, categories and sources are hidden and never summarized.
  - Learning:
    - Interactions table: open / save / more / less / hide.
    - Each signal snapshots the article's topics, categories and source, so learning survives pruning.
  - Summaries now extract specific `topics`, used for matching and trending chips.
  - Summarization queue ordered by personal score.
  - Personal briefings (`briefing.py`, `prompts/briefing.md`): auto after a refresh when the last one is >4h old and ≥3 new stories exist, or on demand. Always covers at least 4 stories (missing ones get their "why it matters" line, since small local models skip some).
  - Live: SSE `/api/news/events` (run / article / breaking / briefing). UI pill "N new stories" and optional desktop notifications.
  - Discover feeds: a catalog of 35 feeds verified live (`catalog.py`).
  - UI: For you / Latest / Top / Saved tabs, briefing card, onboarding card, why-line per story, ▲/▼/hide, interests panel, trending topic chips.
  - CLI: `nvx news follow|mute|unfollow|interests|briefing`, `list --sort for_you`.
  - Stopgap migration: new nullable columns are added automatically to existing databases.
  - Verified in real Edge (Playwright) on a copy of the live DB: personal ranking and reasons, live events, breaking, auto-briefing on Ollama, no horizontal overflow at 390px.

- 2026-10-10: AI layer on seven free cloud providers (`neurovex/llm`).
  - Providers: Cerebras, Groq, Gemini, Mistral, OpenRouter, Cloudflare Workers AI and Hugging Face are first-class. Claude and Ollama are optional.
  - One OpenAI-compatible client with JSON-mode fallback (schema → object → prompt), error classification, and Retry-After parsing (headers, Groq durations, Gemini `retryDelay`).
  - Router: priority or round-robin, overflow by concurrency, cooldowns per failure kind, auth/model disable with re-enable on key change, persisted `llm_calls`, `/api/llm/*`, `nvx llm setup|status|test|models`, and an AI providers panel in the web UI.
  - Secrets come from environment variables, `NEUROVEX_*`, `.env`, or the keychain (keychain optional on servers).
  - Pipeline: when providers are unavailable, articles stay queued without using retries and the run backs off.
  - Verified: 99 offline tests; all 7 live endpoints reached and bad keys classified correctly.

- 2026-10-12: Puku AI added as provider #8 (last in the default chain).
  - Gateway `https://api.sdk.puku.sh/v1` with `X-Api-Key` auth, models `puku-ai-2.8` / `puku-ai-2.7`. Verified live.
  - The key is stored in the keychain as `PUKU_API_KEY`.
  - The account's plan currently has 0 monthly chat requests, so Neurovex sees `quota` and rests Puku for an hour at a time.
  - Plan-quota 429s are now classified as quota (1-hour cooldown), not rate limits.

- 2026-10-14: Reliability + Ask Neurovex.
  - Default chain narrowed to Groq → Puku at your request; the other providers stay in the catalog, off by default.
  - Proactive rate pacing (`llm/ratelimit.py`):
    - Per-provider one-minute window of requests and tokens, plus learning from `x-ratelimit-*` headers.
    - The router waits for capacity (`max_wait_s`) instead of hitting 429s.
    - Background news leaves 2.5K tokens/min for chat.
    - Live result on Groq: 0 rate-limit errors (before: repeated 429s).
  - Provider-side failures (Puku: 500 wrapping an upstream 401) now rest that provider for 15 min instead of being retried every few seconds.
  - Off-list categories from the model are dropped instead of failing the whole summary. Article input trimmed to 8K chars.
  - Skill registry (`core/skills.py`) + 11 news skills + 2 built-ins.
  - Chat engine with a provider-neutral JSON step protocol (D-024), stored conversations, `/api/chat`, `nvx chat`, and an Ask tab in the web UI.
  - Verified live on Groq: one-shot question (1 tool call, 2.8 s); follow/mute via chat; web chat with follow-up in real Edge (desktop + 390px, no errors).

- 2026-10-14: Memory.
  - `neurovex/memory/`: memories table + FTS5 index, store (dedupe, secret filter, BM25), recall (budgeted block), skills (`memory.search`, `memory.list`), `/api/memory`, `nvx memory`, Memory tab, "✦ remembered" notes in chat.
  - Chat steps carry memory ops (add/update/forget); past chat messages are full-text indexed (built from existing chats on first start).
  - Live on Groq: an introduction became 5 atomic memories; a new conversation used them ("as a CS student in Dhaka learning Rust…"); "I graduated last month" became "Graduated in September 2026" without losing anything; "my sister Ayesha lives in Toronto" was stored as a person.
  - Measured: recall ~14 ms, memory block ~48 tokens for 5 memories, 0 extra model calls.

- 2026-10-14: Full test pass before Tasks: 146 tests, 32/32 API checks, a copy of the real DB upgraded cleanly, live chat, and every tab in Edge at 1400 and 390 px with no errors. Fixes from it: the model no longer refreshes news unasked, a provider failure after tools still shows their results, and the prompt got smaller.

- 2026-11-04: Tasks, and Puku only.
  - `neurovex/modules/tasks/` plus a Tasks tab, `nvx tasks`, `/api/tasks`, six chat skills, and `NEUROVEX_TIMEZONE`.
  - Pages now use one live stream, `/api/events`, for news and tasks.
  - Chat costs less per request (D-028).
  - Default provider is Puku through `/v1/messages`, since `/v1/chat/completions` still fails on Puku's side (D-026).
  - Verified live on Puku, on a copy of the real DB: six chat messages back to back all succeeded (2.6–7.7 s each); one article summary (3.4 s).
  - Verified in real Edge at 1400 and 390 px: quick-add preview, groups, Do next, complete and undo on a repeat, edit by quick-add text, reminder with snooze, live badge, no errors, no horizontal overflow.

- 2026-11-04: Today.
  - `neurovex/today/`, a Today tab (now the home tab), `nvx today`, `/api/today`, chat `today.brief`, and a once-a-day announcement.
  - Verified on a copy of the real DB: a real news refresh on Puku (5 of 5 summaries, largest output 623 tokens, plus a written briefing), and chat "brief me" (9.5 s).
  - Verified in real Edge at 1400 and 390 px: every tab, Done and quick-add updating Today, the remembered tab after reload, no errors, no horizontal overflow. The announcement arrived on an open page once and didn't repeat after a reload (desktop width).
  - Not exercised: the desktop-notification pop-up itself, since headless Edge has no notification permission.

- 2026-11-04: X accounts in the news, free through FxTwitter.
  - `news/xsource.py`, a "Follow on X" form in the Sources panel, `nvx news sources add-x`, `POST /api/news/sources/x`.
  - Verified on a copy of the real DB: @OpenAI and @karpathy added through the page in Edge; their own recent posts arrived with replies, reposts and posts over 3 days old left out; Puku summarized a real post without inventing detail. No horizontal overflow at 1400 or 390 px.

- 2026-11-05: News source filter gained "X (all accounts)". X filters also show posts the AI judged not relevant.
- 2026-11-06: UI reworked for speed and density: tighter cards, a one-line List view, a searchable source picker, data fetched per section, the feed left alone while you read, keyboard shortcuts, filters remembered.
- 2026-11-06: Now following 299 X accounts on AI, each checked to exist and be active. A full check takes about 10 minutes every 30, and FxTwitter wrongly answers "no such account" for about 2% of checks; those clear on the next run.

- 2026-11-04: x-tweet-fetcher added as a pinned dependency.
  - Reads X Articles in full (these were being dropped as empty posts) and timelines through an optional Nitter (`NEUROVEX_X_NITTER_URL`).
  - Verified live: a real X Article went from 0 to 4,499 characters; a real refresh through `nvx news refresh` on a copy of the DB. The Nitter path is tested with fakes only.

- 2026-11-04: UI reorganized, dark theme.
  - Side rail (bottom bar on phones): Today, News, Tasks, Ask, Memory, Settings. News has its own sub-tabs, a source filter and day headings on Latest. Today has two columns (plan and news). Settings holds appearance, AI, pipeline, feeds and X accounts.
  - Dark by default, with a quick toggle and Dark / Light / System in Settings, remembered and applied before the page paints. All text colors checked at 4.5:1 contrast or better in both themes.
  - Fixed a race where a slow reply from a previous news tab could overwrite the current one.
  - Verified in real Edge at 1400, 850 and 390 px on a copy of the real DB: every section, both themes, System following the OS setting, source filter, rapid tab switching, no errors, no horizontal overflow.

## Next
1. Watch Puku's quota: Neurovex can't see it. Up to 25 summaries per refresh, every 10 minutes, come out of the same plan as chat. If it runs short, lower `NEUROVEX_NEWS_MAX_SUMMARIES_PER_RUN` or raise `NEUROVEX_NEWS_REFRESH_MINUTES`.
2. Continue with P0.1–P0.3 from docs/PROMPTS.md, folding the news module into the module system (manifest, skills `news.digest` / `news.briefing` / `news.follow`, events onto the shared bus).
3. Candidates: routines ("when I say 'start my day', do X then Y"), evening reflection, API token auth (P0.3, needed before any public server), then the desktop app (Phase 3) and voice (Phase 4), which need installs and your decisions.
4. Later: embeddings (`nomic-embed-text`) for "more like this" by meaning, not just shared topics.

## Known issues
- Memory recall is keyword-based: synonyms ("job" vs "works as") can be missed unless the memory is core or the model searches.
- Puku is a single point of failure: there is no fallback, and no rate headers to pace by (D-026).
- The model can add details you didn't ask for, such as marking a task high priority. Reply and task card show it; `✎` or `tasks.update` changes it.
- Task reminders only reach open pages; a reminder due while none is open waits until one connects. The Today announcement also needs an open page.
- Cross-source duplicates with differently worded headlines aren't merged (e.g. the same "OpenAI engineer arrested" story from The Verge and Hacker News). Needs similarity-based dedupe.
- Chat has no token streaming yet (D-024); each step is one round trip (~1–2 s on Puku, occasionally ~7 s).
- X accounts depend on FxTwitter and x-tweet-fetcher, free third-party tools with no uptime promise. Threads keep only their first post. An account's reposted X Articles are skipped, like other reposts.
- Free tiers are small and change without notice. The router fails over and cools down automatically, but if every provider is limited, summaries pause (articles keep their retries) until the soonest cooldown ends.
- Provider defaults verified 2026-10-10. Model names drift; check with `nvx llm models <id>`.
- Cloudflare's OpenAI-compatible JSON mode isn't documented, so Cloudflare uses prompt-only JSON. Hugging Face gives free accounts very small monthly credits.
- Production: no token auth on the API yet (P0.3). Don't expose the server publicly (`NEUROVEX_ALLOW_REMOTE`) except behind an authenticating reverse proxy.
- Schema: only *nullable* column additions are automatic. Anything else needs Alembic (P0.1) or a DB reset.
- Refresh loop is a plain asyncio task; moves to APScheduler in P6.1.
- No token auth yet (P0.3). The API is protected by the loopback Host check and the `X-Neurovex-Client` header on writes. The SSE stream is read-only.
- Desktop notifications need the web page open (the Tauri shell in Phase 3 will make them native).
