# Job TeleBot — Todo

Full plan: personal job-search assistant on Telegram, powered by OpenAI (default model `gpt-5.4-mini`, set via `OPENAI_MODEL`).
Daily digest of jobs posted in the last 24h + normal AI chat with job-search tools.

## Todo
- [x] Install deps, add `requirements.txt`, `.env.example`, `.gitignore`, `Procfile`
- [x] `db.py` — SQLite: users/profile, onboarding state, chat history, sent jobs
- [x] `job_sources.py` — MyCareersFuture, JSearch, Adzuna fetchers + `filter_recent` + `dedupe`
- [x] `tests/test_job_filter.py` — 24h filter + dedupe tests
- [x] `assistant.py` — AI chat (tools: search_jobs, update_profile, web search), build_profile, rank_and_summarize, web_job_search
- [x] `digest.py` — build daily digest (fetch → 24h filter → unsent → rank/summarise → messages)
- [x] `bot.py` — onboarding flow, commands, chat, hourly scheduler
- [x] Verify: pytest, live fetcher smoke test, offline end-to-end check (mocked OpenAI HTTP + Telegram)
- [x] Review section
- [ ] **You:** fill in `.env` and do a live run (see "Getting started")

## Review

### What was built
| File | What it does |
|---|---|
| `bot.py` | Telegram side. `/start` runs 5 onboarding questions plus an optional resume PDF. Commands: `/profile`, `/editprofile`, `/digest`, `/settime`, `/reset`, `/help`, `/skip`. Free text goes to the AI. An hourly scheduler sends the daily digest. Only `ALLOWED_USER_ID` can use it. |
| `assistant.py` | Every AI call, through the OpenAI Responses API. Chat runs a tool loop with `search_jobs`, `update_profile` and OpenAI's built-in `web_search`. Also `build_profile` (reads the resume PDF), `rank_and_summarize` (keeps relevant jobs, writes a 2-3 sentence summary each) and `web_job_search`. |
| `job_sources.py` | Fetchers for MyCareersFuture (no key), JSearch and Adzuna (each skipped if its key is missing). `filter_recent()` is the **one** place the 24h rule is enforced, and jobs with no posting time are dropped. `dedupe()` merges the same job across sources. |
| `digest.py` | Fetch for each profile keyword plus a web search, filter to the last 24h, drop jobs already sent, have the AI rank and summarise, cap at 10, then format. |
| `db.py` | SQLite: profile, onboarding state, digest hour, chat history (text only), sent-job keys. |

### Things to know
- **24h enforcement:** MyCareersFuture uses the exact `createdAt` timestamp, so reposts keep their original date. JSearch and Adzuna use their posted timestamps. Web results only count if the model saw an explicit posting time, and undated results are dropped. A job is never sent twice.
- **Profile changes:** `/editprofile` redoes setup. You can also just say in chat "also look for quant roles", and the AI updates the profile through the `update_profile` tool, so the digest follows from then on.
- **Missed digests:** if the bot restarts past your digest hour, the digest still goes out at the next hour. It sends at most once a day.
- **JSearch free tier:** capped at `JSEARCH_DAILY_LIMIT` calls a day (default 6, about 180 a month).
- **Switched from Claude to OpenAI:** the user's OpenAI account already has credit. Changes: `assistant.py` now uses `openai.OpenAI().responses.create`, with function tools, the built-in `web_search` tool (OpenAI has no separate web-fetch tool, since its search can open pages itself), `json_schema` structured outputs, and the resume sent as an `input_file`. `requirements.txt` swaps `anthropic` for `openai`, and `.env.example` now asks for `OPENAI_API_KEY` and `OPENAI_MODEL`. Nothing else changed: the Telegram flow, job sources, 24h filter and database are all the same.
- **Cost note:** OpenAI's web search adds a per-search fee. It runs in chat when needed and once a day for the digest.
- **Not verified live yet:** no OpenAI key or bot token was available, so the AI calls were tested against a mocked HTTP layer using the real OpenAI SDK, which checks the request shapes. Job fetching was tested live: MyCareersFuture returned 53 real jobs from the last 72h.

### Getting started (local)
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `OPENAI_API_KEY` and `ALLOWED_USER_ID` (get your ID from @userinfobot). Optionally add `RAPIDAPI_KEY` and the Adzuna keys.
3. `python bot.py`, then send `/start` to your bot in Telegram.
4. Run `python -m pytest -q tests` for the filter tests.

### Deploying (Railway)
1. Push this folder to a **private** GitHub repo (`.env` is git-ignored, so never commit it).
2. Railway: New Project → Deploy from GitHub. It detects Python and runs the `Procfile` worker (`python bot.py`).
3. Add a **Volume** mounted at `/data`, and set the variable `DB_PATH=/data/bot.db` so your profile survives redeploys.
4. Add the other `.env` values as Railway variables. Only run **one** instance, because Telegram polling allows only one.
