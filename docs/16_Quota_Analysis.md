# Quota & Resource Analysis

The project runs entirely on free-tier infrastructure and free-tier LLM APIs, so **direct monetary cost is $0**. That does not make the project unconstrained — it moves the constraint from dollars to **API requests per day**. This document models that constraint the way a cost model would model spend.

## Why requests, not tokens
Paid APIs meter per token, so the optimization target is fewer tokens — especially output tokens. Free tiers meter per *request*, with caps on requests per minute and per day. The optimization target is therefore **fewer API calls per review**, which is a different design decision and drives ADR-006.

## Request Model
Requests per review depend on:
- Files changed per PR (typically ~5, after skipping binaries, lockfiles, and generated code)
- Whether classifier calls are batched (1 call) or per-file (N calls)
- Whether judge calls are batched per file (N calls) or issued per finding (often 2N)
- Retries caused by schema-invalid LLM output

## Formula
`Requests per PR ≈ classifier calls + generation calls (1 per file) + judge calls + retries`

`Requests per eval run ≈ requests per PR × number of curated PRs`

## Estimates (per PR, ~5 files, ~10 raw findings)

| Stage | Naive | Batched (ADR-006) |
|---|---:|---:|
| Classify | 5 | 1 |
| Generate | 5 | 5 |
| Judge | 10 | 1–5 |
| **Total per review** | **~20** | **~7** |
| **Total per 40-PR eval run** | **~800** | **~280** |

The eval run figure is the number that matters. Weeks 6–7 involve re-running the harness after every prompt or threshold change, so requests-per-run directly determines how many tuning iterations are possible before the deadline.

## Infrastructure
| Component | Cost | Real constraint |
|---|---|---|
| LLM inference | $0 | Free-tier requests/minute and requests/day |
| Hosting (Render free tier) | $0 | Spins down after ~15 min idle; cold start ~50s, which consumes most of the ~60s review NFR |
| Database (Supabase free tier) | $0 | Pauses after ~7 days of inactivity and needs a manual wake — a demo-day risk |
| GitHub API | $0 | ~5,000 requests/hour per installation; connected-file code search is the endpoint most likely to approach it |

## Optimization
- Batch classifier and judge calls (ADR-006) — the single largest lever on quota.
- Review only changed hunks + targeted connected files, never full files; cap connected-file context per file so one widely-referenced symbol cannot balloon a request.
- Skip binary files, lockfiles, and generated code entirely.
- Cap files reviewed per PR so a single large PR cannot exhaust a daily quota.
- Spread pipeline stages across two providers where quota is tight — the provider adapter (ADR-003) makes this configuration, not code.
- Reject providers that fail the structured-output validity gate; malformed JSON forces retries, and retries are wasted quota.

## Instrumentation
Every provider response carries token usage. Log per call, keyed to the review ID: provider, model version, prompt-template version, input tokens, output tokens, and **request count**. Requests-per-review is then a query rather than an estimate, and `GET /quota-report` returns measured quota consumption. Track this from week 1, not retroactively.
