# Quota & Resource Analysis

Every stage runs on a free tier, so direct spend is **$0**. The constraint is
not money and not request count: it is **tokens, per model id, per minute and
per day**.

## The unit is tokens

Groq's free tier meters two windows against each model id:

| Window | Free tier | Developer tier |
|---|---:|---:|
| Tokens per minute | 8,000 | 250,000 |
| Tokens per day | 200,000 | no limit |

Requests per minute exist as a limit too, and nothing here has ever reached
one. Both walls this project has actually hit were token walls. The second is
the one that stops work for the rest of the day:

```
rate_limit_exceeded: tokens per day (TPD): Limit 200000, Used 198022
```

This matters because it inverts the optimisation target. Fewer calls is worth
little on its own; fewer *tokens* is worth everything. Batching still helps,
but for a token reason rather than a request one — a system prompt and a set
of tool schemas are resent on every call, so collapsing five calls into one
stops paying that overhead five times.

## What a review actually costs

Measured on review 13, a four-file pull request through the agentic pipeline:

| | Measured |
|---|---:|
| Model calls | 18 |
| Specialist tokens | 64,516 |
| Wall clock | 428s |
| Equivalent paid cost | ≈$0.017 |

Nearly all of the 428 seconds was waiting for budget, not waiting for a model.
65,000 tokens drawn 8,000 at a time is eight minutes of enforced idling before
any thinking happens. The review was never slow; the tier was.

A run that wakes the agentic verifier roughly doubles the tokens, to ≈$0.035.

### Where the tokens go

| Stage | Calls | Loop | Tools | Share of tokens |
|---|---:|---|---|---|
| Classification | 1 | no | no | small |
| Triage | 0 | — | — | none, it reads existing labels |
| Specialists (two, concurrent) | many | yes | yes | **most of it** |
| Verification, cheap path | 1 | no | no | small |
| Verification, agentic path | many | yes | yes | large when it wakes |
| Summary | 1 | no | no | small |

The loops dominate, and the reason is resend cost: every turn resends the
entire conversation so far, including every tool result already returned. A
six-turn agent does not cost six calls' worth of tokens, it costs something
closer to the sum of a growing prefix. This is why the caps in
`src/agents/loop.py` are token caps as much as turn caps:

| Cap | Value |
|---|---:|
| `MAX_TURNS` | 6 |
| `MAX_TOOL_CALLS_PER_TURN` | 5 |
| `MAX_TOKENS` | 80,000 |

## What the daily limit means in practice

200,000 tokens per day against ≈65,000 per review is **about three reviews a
day** on the generation model.

Two things soften that, one by design and one by accident:

- **The limit is per model id, not per account.** Generation runs on
  `openai/gpt-oss-120b` and verification on `qwen/qwen3.8-27b`, so each draws
  on its own 200,000. The different-family judge was chosen for
  independence — a model asked to check its own work mostly agrees with
  itself — and the separate quota bucket is a side benefit worth naming.
- **Verification is cheap on the common path.** `judge_findings` sends one
  call for all findings in a review, not one per finding.

What it does **not** soften: a full evaluation run. Four cases at ≈65,000
tokens is ≈260,000, which exceeds a single model id's daily allowance. **The
eval harness cannot complete a full run in one day on the free tier.** Since
tuning means re-running the harness after every prompt or threshold change,
this is the single hardest limit on iteration speed, and it is the reason the
paid tier is under consideration at all.

## Reserving rather than reacting

`src/llm/budget.py` estimates a request's cost and subtracts it from the
remaining budget **before** sending, then corrects the estimate from the
response headers.

The estimate is deliberately pessimistic: characters divided by four, plus a
`COMPLETION_ALLOWANCE` of 1,200, because the answer is charged too and an
estimate that ignores it always runs under. Tool schemas are included in the
count — they are easily a thousand tokens and omitting them was one of the
original undercounts.

Two distinctions the limiter has to make, both learned from a live failure:

- **A minute is not a day.** A per-minute refusal is waited out. A per-day
  refusal is terminal, detected by the `tokens per day` marker, and fails
  immediately rather than retrying three times into the same wall.
- **One limiter per model id.** A single shared limiter made a wait on the
  generation model stall the judge, which had budget to spare.

## Reducing tokens

| Lever | Effect |
|---|---|
| Bounded loops: turns, calls per turn, tokens | caps the resend multiplier, the largest single factor |
| Skip lockfiles, vendored, generated and binary files | removes whole files before any call |
| `MAX_CHANGED_LINES_PER_FILE = 500` | one enormous file cannot consume a day |
| Diff hunks plus targeted references, never whole files | context is retrieved, not dumped |
| Batched classification, one call for all files | stops resending the system prompt per file |
| One judge call for all findings | same, for verification |
| Reject providers that return invalid JSON | a retry is a second full payment for one answer |

## Infrastructure

| Component | Cost | Real constraint |
|---|---|---|
| LLM inference | $0 | tokens per minute and per day, per model id |
| Database (Neon free tier) | $0 | idle connections are closed by the provider — a session held open across a ten-minute review was dropped mid-review |
| Hosting | — | **not deployed**; runs on a laptop |
| GitHub API | $0 | ~5,000 requests/hour per installation, never approached |

## Instrumentation, and the gap

`QuotaCounter` in `src/llm/budget.py` counts requests, prompt tokens,
completion tokens, retries and seconds spent waiting. The pipelines read it
before and after a review to report model calls, and the evaluation harness
writes per-case figures to its JSON report.

It is still a process-global in memory, but it is no longer the only copy.
Each review now writes its `model_calls`, `prompt_tokens`,
`completion_tokens` and `seconds` to its own row, and each finding records
the model and prompt version that produced it. Cost per review is a query:

```sql
SELECT avg(model_calls), avg(prompt_tokens + completion_tokens), avg(seconds)
FROM reviews WHERE status = 'posted';
```

There is still no `/quota-report` endpoint; the numbers are read with SQL.
That is adequate for one developer and not for anyone else, which is the
honest reason it is absent rather than a decision that it is unnecessary.
