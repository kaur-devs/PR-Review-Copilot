# Observability

What exists is a console trace and structured logs. There are no metrics,
no alerting, and no distributed tracing. The previous version of this
document described all three as though they were running.

## The review trace

`src/common/trace.py` prints each stage of a review to stdout as it
happens, which is what every measurement in this project has been read
from:

```
> CHANGED FILES    8 changed · 8 reviewable · 0 skipped
> TRIAGE           wrong, robust
> SPECIALISTS      12 candidates · 16 agents over 8 file(s) · 71 turns
> VERIFY           6 investigated · 11 supported, 1 contradicted
> GATE             9 to post · 1 held back
> SUGGESTIONS      6 offered, 1 refused
> POSTED           review 5896062365
  REVIEW 23 DONE · 11 posted, 1 held · 38.4s · 115 model calls
```

Every refusal, hold and contradiction prints its reason. That was not
always true: the agentic pipeline printed suggestion counts without
reasons for four reviews, having lost those two lines when the pipelines
were split, so "4 refused" said nothing about why.

## Logs

Logged on every review: delivery id, repository, pull request number,
review id, stage, model, wait reasons, and error code. `httpx` is held at
WARNING so its per-request lines do not drown the trace.

Not logged: pull request source beyond what a specific failure needs.

## What is measured, and where it lives

| | Where |
|---|---|
| Calls, tokens and seconds per review | `reviews` table, per row |
| Model, prompt version, finder per finding | `findings` table |
| Acceptance rate per category per repository | `outcomes`, queried by suppression |
| Recall, precision, F1 | `src/evaluation/`, written to `eval/reports/` |

Cost per review is a SQL query rather than a log grep:

```sql
SELECT avg(model_calls), avg(prompt_tokens + completion_tokens), avg(seconds)
FROM reviews WHERE status = 'posted';
```

## What is missing

- **No metrics backend.** Nothing counts webhook receipt rate, stage
  latency or failure rate over time. Each is derivable from the database
  by hand.
- **No alerting.** A repeated failure, an expired token or a spent daily
  quota is discovered by reading the terminal.
- **No request id on the way out.** Errors returned to GitHub carry a
  generated `requestId`, but it is not written to the log, so it cannot
  be used to find the request it names.

The first two need somewhere to run before they mean anything, which puts
them behind deployment.
