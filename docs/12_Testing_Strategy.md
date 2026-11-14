# Testing Strategy

677 tests, about 19 seconds. No test reaches the network or the development
database.

```bash
.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest tests/unit   # no Postgres needed
```

## Three levels, by what a test needs to run

| Folder | Needs | Covers |
|---|---|---|
| `tests/unit/` | nothing | diffs, gates, symbols, languages, prompts, suggestions, scoring |
| `tests/integration/` | fake HTTP | the model client, the agent loop, specialists, the judge, GitHub clients |
| `tests/database/` | Postgres | the webhook endpoint, the pipeline, the graph, persistence, recovery, outcomes |

The folder is the marker. `empty_tables` reads the path and skips the
truncate outside `tests/database/`, so the unit suite runs with no database
at all.

## How the outside world is faked

| Technique | Stands in for |
|---|---|
| `httpx.MockTransport` | GitHub and the model provider |
| Separate test database | `pr_review_copilot_test`, truncated between tests |
| Generated RSA key | real signing, thrown away after the run |

`conftest.py` sets the environment before `src.config` is imported, and
**refuses to run when `TEST_DATABASE_URL` matches `DATABASE_URL`** — the
suite drops and truncates every table, so pointing it at the development
database would erase the review history.

Shared fakes are in `tests/fakes.py`; see [tests/README.md](../tests/README.md).

## Conventions

One test file per source module. Test names are sentences, so a run reads as
a list of behaviours:

```
test_a_sound_suggestion_survives_the_whole_review
test_a_low_severity_finding_never_reaches_the_verifier
test_a_spent_daily_budget_fails_at_once_instead_of_retrying
```

A docstring on a test says why the behaviour matters, not what the code does.
Several name the live incident that produced them, because a test written
after a real failure is worth more when it says which one.

## Scenarios, and whether they are covered

| Scenario | Expected | Status |
|---|---|---|
| Valid pull request webhook | review runs | covered |
| Invalid signature | rejected, nothing stored | covered |
| Missing or malformed headers | `400`, nothing stored | covered |
| Empty diff | skipped, no findings | covered |
| Lockfiles, vendored and generated files | skipped with a reason | covered |
| Unusable model output | stage fails, review degrades | covered |
| Model unavailable | `LLM_NOT_CONFIGURED`, graceful | covered |
| Judge contradicts a finding | dropped before posting | covered |
| Judge cannot check a finding | kept at 0.7 confidence | covered |
| Same delivery twice | second recorded `duplicate`, one review | covered |
| Two deliveries, same commit | one review claimed, second `duplicate` | covered |
| Unhandled action such as `labeled` | recorded `ignored`, `200` | covered |
| New commit mid-review | older review `superseded`, not posted | covered |
| Restart mid-review | found at startup and re-run; `failed` past the attempt cap | covered |
| Rate limited by the model | waits for the window, retries | covered |
| Daily token budget spent | fails at once rather than retrying | covered |
| Hallucinated tool name | corrected, run continues | covered |
| Tool arguments the schema refuses | corrected, run continues | covered |
| The agent will not answer at the cap | asked again with no tool calls to continue | covered |
| A linter rule that is wrong for the framework | never selected, pinned by test | covered |
| A category a repository ignores | suppressed after ten posted findings | covered |
| A repository's own thresholds | read from `.pr-review.yml`, broken file ignored | covered |
| Two findings about the same block | merged into one comment | covered |
| Crash after GitHub accepted, before it was recorded | nothing posted twice | **not covered — see below** |

### The double-post window

A review posts, GitHub accepts, and the process dies before
`record_posted_review` runs. The findings are stored but `posted` is false
and `github_comment_id` is null.

Recovery then re-runs that review. `previously_posted` deliberately excludes
the current review (`Review.id != review_id`), and nothing asks GitHub what
is already there — so the same comments can be posted a second time.

The window is small and nothing has hit it, but it is a real gap rather than
a tested behaviour. Closing it means either consulting GitHub for an existing
review on that commit before posting, or recording intent before the call
instead of after.

## What the suite does not reach

Every test is single-threaded against fakes. The failures found in live
running — a rate limiter that reported instead of reserving, a database
session held open across a ten-minute review, a daily budget retried as
though it were a per-minute one — are not reachable from here.

Those were found by running the thing, not by the suite. The suite's job is
to stop them coming back, and it does: each has a test now. But a green run
means the known behaviours still hold, not that the system works under
concurrency.

**Concurrency tests are the clearest missing level.** Two specialists sharing
one token budget is the exact shape of the bug that cost the most time.

## Evaluation

Separate from the test suite: `src/evaluation/` replays labelled pull
requests and scores recall, precision and context recall, with ablations for
context, judge and templates.

| | Planned | Actual |
|---|---|---|
| Cases | 30–50 historical pull requests | **5** |
| Languages | whatever the repositories use | Python and TypeScript |

Five cases still cannot separate a real improvement from noise. What has
changed is that the languages gap is closed and the harness now runs the
pipeline the service actually uses — it called the single-pass reviewer
unconditionally until recently, so every number it produced described a
system that had been replaced.

The flagpole case is 246 lines across 8 files, Python API and TSX
dashboard, with 16 labelled defects and five deliberately clean sections
that count against precision if commented on. Latest run: **56% recall,
82% precision, 67% F1**.

Scoring allows a defect more than one anchor. A cross-file break has two
ends — the rename and the line that still reads the old name — and
counting only one of them scored a correct finding as a miss and a false
positive at once. Fixing that moved the same run from 35%/67% to
56%/82% without touching the reviewer.

Competitor numbers are the calibration, not the target: Qodo 60.1% F1,
CodeRabbit 51.2% F1, Greptile's self-reported 82% bug-catch rate.
