# Deployment

**Status: not deployed.** The service runs with `uvicorn` on a laptop,
reached through a tunnel, and the GitHub App's webhook URL points at that
tunnel. There is no `Dockerfile`, no `render.yaml`, no `Procfile`.

This is the one requirement that is binary rather than a matter of degree,
and it is the gap between a reviewer that works and a product somebody else
can install. TR-016 tracks it.

This page is the plan for closing it.

## What is running today

| | |
|---|---|
| Process | `uvicorn src.main:app --reload --port 8000` |
| Reachable at | a tunnel, re-pointed whenever it restarts |
| Database | Neon, hosted, shared with development |
| Models | Groq, Developer plan, 250K tokens/min per model |
| Secrets | `.env`, gitignored; the App key as a file path |
| Schema | 7 migrations, head `d1f3a07c45b2` |
| Tests | 679, passing |

## The target shape

```
Render (free web service)  ──▶  Neon (free Postgres)
      │
      │ keep-alive ping every 5 min (the free tier sleeps after 15)
      ▼
 external cron  ──▶  GET /health
```

Single process. One FastAPI app under uvicorn, one PostgreSQL database.

Four things have to be true before it works at all:

1. GitHub can reach it at a fixed address
2. The secrets arrive without being written into the code
3. It can reach Neon
4. **It answers GitHub within 10 seconds**

The fourth is the awkward one. GitHub waits 10 seconds and never retries.
The free tier sleeps an idle service after fifteen minutes and takes about a
minute to wake, so a sleeping service misses a delivery it will never be told
about again. An external pinger on `/health` keeps it awake. `/health`
touches neither the database nor a model, so the checks cost nothing.

## Why Render, and what was rejected

Decided 2026-11-24, confirmed 2026-11-25. **Not to be reopened without new
information.**

| Option | Why it lost |
|---|---|
| Oracle Cloud Always Free | Genuinely always-on and the best answer to constraint 4, but wants a card at signup, needs Linux administration, and ARM instances are reported reclaimable after ~7 idle days. |
| Google Cloud Run | Cold start would plausibly fit inside 10 s (process import is 0.87 s), and 2M requests/month are free — but it needs a billing account. |
| Heroku via the GitHub Student Pack | $13/month credit for 24 months, and a $7 Basic dyno is always on, which would delete the pinger entirely. Considered 2026-11-25 and declined. |
| DigitalOcean | No longer in the Student Pack; the $200 credit ended around July 2026. |
| Render / Railway / Fly.io | None are in the Student Pack, so it offers nothing on this path. |

**The accepted cost of choosing Render free:** a delivery that lands while
the service is asleep or restarting is lost silently. The pinger shrinks that
window; it does not close it. This is a known, accepted risk, not an
oversight — see *Known holes* below.

## The plan, in order

Steps 1 to 4 are code and run locally. Steps 5 to 10 are configuration.

| # | Step | Where | Done when |
|---|---|---|---|
| 1 | Accept the App private key as **text** as well as a path | code | tests pass, app still starts locally |
| 2 | Pin every version in `requirements.txt` | code | every line reads `name==x.y.z` |
| 3 | Write the `Dockerfile` | code | it builds |
| 4 | Build and run the container locally | local | `/health` answers `{"status": "ok"}` |
| 5 | Create the Render web service (Docker env, Free plan) | browser | first build completes |
| 6 | Add the environment variables | browser | the app boots without a `RuntimeError` |
| 7 | `alembic upgrade head` against the production database | local | nothing left to upgrade |
| 8 | Repoint the GitHub App webhook URL | browser | a ping delivery returns 204 |
| 9 | Open a real pull request and watch the logs | browser | a review is posted |
| 10 | External cron on `/health` every 5 minutes | browser | no cold start after an idle hour |

### Step 1 — the private key

`src/config.py` lists `GITHUB_APP_PRIVATE_KEY_PATH` in `REQUIRED_VARS` and
refuses to start unless that path is a real file. A hosted service has no
such file, so the key has to arrive as `GITHUB_APP_PRIVATE_KEY` — the PEM
text, newlines and all.

Two ways to take it, and they are not equivalent:

- **Write it to a temp file at startup** and leave `private_key_path` alone.
  Nothing downstream changes, but the key then sits on disk for the life of
  the process, which defeats the on-demand read that `read_private_key()`
  exists to provide.
- **Make `read_private_key()` accept either source.** The key never touches
  disk and the on-demand property survives. Costs a less tidy dataclass and
  a `REQUIRED_VARS` that demands *one of two* variables rather than one, and
  says which is missing when neither is set.

The second is preferred. Either way the PEM must keep its
`-----BEGIN RSA PRIVATE KEY-----` header and trailing newline, or
`pyjwt[crypto]` raises when the first webhook arrives rather than at boot.

**This is the only code change on the list.** In particular, nothing
hard-codes a port: `src/main.py` never binds one, so the port is a `CMD`
detail in the Dockerfile reading `$PORT`, not an edit to the application.

### Step 2 — pinning

**Done 2026-11-25.** Unpinned, a build installs whatever is newest on the day
it runs, which may not be what the 686 tests pass against — and that surfaces
as a crash rather than a failing test.

Two files, because 18 direct dependencies pull in about 50 more and those
break a build just as easily:

| File | Holds | Read by |
|---|---|---|
| `requirements.txt` | the 18 direct dependencies, pinned, with the comments explaining each one | people |
| `requirements.lock.txt` | all 71, generated by `pip freeze` | **the Dockerfile** |

A flat freeze into one file would have been reproducible too, but it would
have destroyed the only record of *why* each dependency is there and the
distinction between what was chosen and what came along for the ride.
Regenerate the lock after any dependency change:

```
.venv/bin/pip freeze > requirements.lock.txt
```

Two things the audit turned up:

- **`pytest-asyncio` was missing.** `pytest.ini` sets `asyncio_mode = auto`,
  which is that package's option, so a fresh clone could not run the suite.
- **`ruff` is a runtime dependency, not a development tool.**
  `src/analysis/lint.py` shells out to it during every review — it is where
  the `ASYNC` and `E711` findings come from, the half of the reviewer that
  costs no tokens. It looks for `.venv/bin/ruff`, then falls back to `PATH`.
  A container has no `.venv`, so it must be on `PATH`. If it finds neither,
  `run()` returns no findings **and logs nothing at all**, so the
  deterministic half of the reviewer disappears in production without a
  single line in the log to say so. It must never be stripped from the image
  as a test-only package.

Pinning does **not** de-risk step 3. Version numbers are
platform-independent; whether `tree-sitter-typescript==0.23.2` ships a cp314
*manylinux* wheel is a separate question that only the Docker build answers.
What pinning buys is that step 3 fails identically every time instead of
differently each day.

### Step 3 — the image

Python **3.14** to match `pyproject.toml`'s `target-version = "py314"` and
the 3.14.5 venv. The likeliest first-build failure is a missing cp314
manylinux wheel for `tree-sitter-typescript==0.23.2`, the oldest of the four
grammars; if it falls back to compiling, the image needs a C toolchain.

### Step 6 — the environment variables

The app refuses to start without the first five.

| Variable | Notes |
|---|---|
| `GITHUB_WEBHOOK_SECRET` | must match the App settings page exactly |
| `GITHUB_APP_ID` | |
| `GITHUB_CLIENT_ID` | the issuer claim when signing as the App |
| `GITHUB_APP_PRIVATE_KEY` | the PEM contents, pasted with real newlines |
| `DATABASE_URL` | the Neon connection string |
| `LLM_API_KEY` | Groq |
| `AGENTIC_PIPELINE` | **`1`** — it defaults to `0`, which is the old single-pass reviewer |
| `PIPELINE_TRACE` | `0` for production logging, `1` while demonstrating |
| `LANGSMITH_TRACING` | **`false`** — see below |

LangGraph brings in `langsmith`, which can send traces to a third-party
service. It is off unless `LANGSMITH_TRACING` or `LANGCHAIN_TRACING_V2` is
set, and `langsmith.utils.tracing_is_enabled()` confirms it is off today.
Set it to `false` explicitly anyway. This service reads other people's
private source code, so where that code can travel should be a decision in
writing rather than a library default that a version bump could change.

These go in Render's Environment page only, never in the repository. Anyone
holding the private key can act as the App. `.env` and `*.pem` are gitignored
and have never been committed.

### Step 9 — reading a failure

GitHub App settings → Advanced lists every delivery, the response it got, and
a **Redeliver** button.

| Response | Meaning |
|---|---|
| 401 | the webhook secret differs between GitHub and Render |
| timeout | the service was asleep — step 10 |
| 502, or nothing | it crashed at startup; the Render log names the missing variable |
| 200, no comments yet | it worked; the review is running in the background |

## What deployment has to survive

Known from running it, not guessed.

- **Neon closes idle connections.** A review spends most of its time waiting
  on a token budget, and ten minutes is ordinary. A transaction held open
  across that wait dies. This already caused one mid-review failure, which is
  why `src/pipeline/record.py` opens a session, does one thing and closes it,
  never holding one across a review.
- **Connections go through a pooler**, so a prepared statement cached on one
  connection may not exist on the next. `src/db/url.py` sets
  `statement_cache_size=0` when it sees `-pooler` in the hostname.
- **Recovery runs on every boot.** `start_recovery()` scans for interrupted
  reviews each time the process starts. On a platform that recycles
  containers this runs far more often than it was designed for — which is
  also what rescues a review killed mid-flight.
- **One container, one rate limiter.** The token budget in `src/llm/budget.py`
  is per process. Two instances would each believe they own the whole 250K
  per minute, so **instance count must stay at 1.**
- **The webhook URL moves once.** Deliveries sent to the old tunnel are lost,
  not retried. Pointing it back at the tunnel is the rollback while fixing
  something.

## Known holes

Stated rather than hidden, because each has a reason:

| Hole | Why it is tolerated |
|---|---|
| A delivery arriving while asleep or restarting is lost, silently | The pinger makes it rare. Closing it properly needs either a paid always-on instance or a reconciliation sweep that polls for un-reviewed open pull requests — neither is in scope. |
| A permanently-awake free service burns ~720 of Render's 750 free instance-hours a month | It fits, with ~30 hours of slack, but it is the whole allowance: no second free service. |
| Render does not support keep-alive pings | It works and is widely relied on. If the rules tighten, the fix is the $7/month instance. |
| `langgraph-checkpoint-postgres` is pinned but never wired in | `graph()` compiles without a checkpointer, so recovery re-runs a review from the beginning instead of resuming at its last stage. |

## No GPU, by constraint

All inference is through hosted APIs on a free tier. Nothing is fine-tuned or
trained. The provider sits behind our own client, so moving to another is four
environment variables rather than a rewrite.

## Rollback

Repoint the GitHub App webhook URL at the tunnel; the laptop takes over
again. Schema changes are versioned with Alembic and every migration has a
tested `downgrade()`.
