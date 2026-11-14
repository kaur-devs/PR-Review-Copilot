# Low-Level Design

How the service is built, package by package. The shape is in
[06_HLD.md](06_HLD.md); the review stages are in
[29_Agentic_Architecture.md](29_Agentic_Architecture.md). This is the layer
below both: what lives where, what may import what, and the rules that keep
one failure from becoming two.

## 1. Module map

```text
src/
  main.py            FastAPI application, startup, /health
  config.py          settings, read once and validated at startup
  recovery.py        picks up reviews interrupted by a restart

  webhooks/          receiving events from GitHub
    signature.py       HMAC-SHA256 verification
    events.py          which events matter, and the fields kept
    router.py          POST /webhooks/github

  pipeline/          running one review
    record.py          database and GitHub side; posts the result
    graph.py           the stages, as a LangGraph state graph
    agentic.py         opens resources, invokes the graph
    single_pass.py     the one-call alternative
    prepare.py         fetch and filter, shared by both

  agents/            the tool loop and what runs inside it
    loop.py            send, run tools, send again, stop
    tools.py           read_file, grep, definitions, references, tests
    prompts.py         the two specialist prompts
    specialists.py     runs them concurrently, reads their findings
    triage.py          which specialists to wake; no model call
    summary.py         the opening comment
    answers.py         pulling JSON out of a reply
    measure.py         turn and token measurement harness

  llm/               talking to a model
    provider.py        the HTTP client
    budget.py          per-model token budgets, reserved before sending
    errors.py          what can go wrong, by what the caller does about it
    messages.py        tool calls and completions, on the wire
    settings.py        where to send, how hard to try

  github/            talking to GitHub
    auth.py            App JWT, then installation token
    client.py          requests, pagination, headers
    review.py          building and posting one review

  diff/              working out what changed
    files.py           fetch the changed file list
    skip.py            which files are worth reviewing
    parse.py           patch text into hunks and line numbers

  context/           connected-file context
    snapshot.py        the repository at the reviewed commit
    symbols.py         what changed, as definitions
    references.py      who uses them, with confidence
    languages.py       tree-sitter queries per language
    bundle.py          ranked, budgeted context
    gather.py          the entry point

  classification/    labelling each changed file
  judge/             verifying findings, cheaply or by looking
  filter/            gates, repeats, suggestions
  review/            findings, prompts, generation for the single pass
  outcomes/          recording what the developer did
  db/                models, queries, session, url
  evaluation/        replaying labelled pull requests
  common/            trace.py, the structured terminal trace
```

Six tables: `installations`, `repos`, `reviews`, `webhook_events`,
`findings`, `outcomes`.

### Dependency rules

Enforced by convention. A violation is a design smell, not a build failure.

| Layer | May import | May not import |
|---|---|---|
| `config` | nothing from `src` | anything |
| `webhooks/signature` | nothing from `src` | anything, including FastAPI |
| `webhooks/events` | nothing from `src` | anything |
| `llm/*` | `config` | everything else |
| `db/*` | `config`, `webhooks.events` | `github`, `diff`, `pipeline` |
| `github/*` | `config` | `db`, `diff`, `pipeline` |
| `diff/*` | `github.client` | `db`, `pipeline` |
| `context/*`, `agents/*` | `llm`, `diff`, `github.client` | `db`, `pipeline` |
| `pipeline/*` | everything below | `webhooks.router` |
| `webhooks/router` | `config`, `db`, `pipeline` | `diff`, `github` directly |

Three rules carry weight:

- **`signature.py` imports no framework**, so it is testable without a web
  server.
- **`pipeline/` never imports the router**, so the evaluation harness drives
  a review with no HTTP involved.
- **Nothing outside `llm/` makes an HTTP call to a model.** Every request
  passes one place, which is where retries, token budgets and tool-call
  parsing live.

## 2. Configuration

`src/config.py` reads `.env` once, validates the required variables at
startup, and hands out a frozen `Settings`. Discovering a missing secret when
the first pull request arrives is far worse than refusing to start.

Required: `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_CLIENT_ID`,
`GITHUB_APP_PRIVATE_KEY_PATH`, `DATABASE_URL`.

The model is not among them. `LLM_API_KEY` is read where it is used, so a
service without one still starts and skips reviews with a clear status rather
than failing to boot.

Switches, all read with `os.getenv` at the point of use:
`AGENTIC_PIPELINE`, `CONTEXT_ENABLED`, `JUDGE_ENABLED`, `SUGGESTIONS_ENABLED`,
`TEMPLATES_ENABLED`, `RECOVERY_ON_STARTUP`, `PIPELINE_TRACE`.

## 3. Idempotency

Two independent keys, catching different problems:

| Key | Catches | Typical cause |
|---|---|---|
| `webhook_events.delivery_id` | the same message twice | the Redeliver button |
| `reviews(repo_id, pr_number, head_sha)` | two messages about the same code | reopening a pull request with no new commits |

Both answer `200 duplicate`. When the second fires, the delivery row's
`outcome` is corrected from `accepted` to `duplicate`, so the record stays
honest.

Claiming is an `INSERT ... ON CONFLICT DO NOTHING ... RETURNING id`. Whoever
writes the row owns the work; the loser gets nothing back. Checking first in
Python would race when two messages arrive together.

Review ids skip numbers, because a rejected insert still consumes a sequence
value. Expected, not a lost row.

## 4. Review state machine

```
received ──> processing ──> posting ──> posted
   │             │              │
   │             ├──> skipped   └──> failed
   │             ├──> no_findings
   │             ├──> all_repeats
   │             ├──> failed ──> processing   (retry, under the attempt cap)
   │             └──> superseded
   └──> superseded
```

| Status | Meaning |
|---|---|
| `received` | commit claimed, nothing started |
| `processing` | the review is running |
| `posting` | findings being sent to GitHub |
| `posted` | comments are live |
| `skipped` | nothing worth reviewing |
| `no_findings` | reviewed, nothing to say |
| `all_repeats` | everything found had already been posted |
| `superseded` | a newer commit made this review irrelevant |
| `failed` | `error_code` says why |

`all_repeats` is deliberately not `no_findings`: a review that found the same
three bugs again is a different outcome from one that found nothing.

`superseded` is set when a newer commit is claimed for the same pull request.
A review already in `posting` or past it is left alone, because comments
cannot be taken back. A running review learns it was superseded when its next
status update matches no row, and stops before posting.

### Ordering rules

1. A delivery is recorded before any work begins.
2. A commit is claimed before the review is scheduled.
3. The status leaves `received` before any external call.
4. `attempts` increments before the status change, so a crash still counts.
5. Findings are stored before posting, so a posting failure loses nothing.
6. `github_review_id` is set only after GitHub accepts.
7. **No database session is held across the review itself.** A review can
   take minutes, and an idle connection is closed by the provider underneath
   it. The database is touched in short bursts: claim, then the review with
   nothing open, then store and post.

## 5. Error handling

| Code | HTTP | Raised in | Stored |
|---|---|---|---|
| `WEBHOOK_SIGNATURE_INVALID` | 401 | router | no |
| `WEBHOOK_HEADERS_MISSING` | 400 | router | no |
| `WEBHOOK_PAYLOAD_MALFORMED` | 400 | router | no |
| `WEBHOOK_PAYLOAD_INCOMPLETE` | 400 | router | no |
| `PIPELINE_UNEXPECTED` | — | pipeline | `reviews.error_code` |
| `COMMENT_POST_FAILED` | — | pipeline | `reviews.error_code` |
| `LLM_NOT_CONFIGURED` | — | pipeline | `reviews.error_code` |
| `REVIEW_INTERRUPTED` | — | recovery | `reviews.error_code` |

Nothing that fails verification is written. A review failure degrades the
review, never the service.

A failure is recorded on **a fresh database session**, because the session
that raised may be the cause: once its connection has gone, every further
statement on it fails, and the record of why would be lost with it.

Model failures are classified by what the caller should do:

| Error | Meaning |
|---|---|
| `RateLimited` | the minute's budget; wait and retry |
| `DailyQuotaExhausted` | the day's budget; fail now, retrying is silence |
| `UnknownTool` | the model invented a tool; tell it the real names |
| `InvalidResponse` | unreadable reply |
| `LLMNotConfigured` | no key |

## 6. Recovery

A review interrupted by a restart is left in `processing` with nothing
working on it. GitHub does not retry, and the uniqueness rule refuses a
second claim on the same commit, so nothing else will ever move it.

At startup, `recovery.py` scans for reviews stuck past a fifteen-minute
timeout, takes them over one at a time, and stops at a small number per
start. The rest are found at the next start, which is the point of scanning
rather than keeping a queue. A review out of attempts is marked `failed` so
the row stops claiming to be in progress.

The scan is started, not awaited. GitHub allows ten seconds per delivery, so
being slow to listen loses messages outright.

## 7. Testing

677 tests, about 19 seconds. **No test reaches the network or the
development database.**

| Folder | Needs | Count |
|---|---|---|
| `tests/unit/` | nothing | 15 files |
| `tests/integration/` | fake HTTP | 13 files |
| `tests/database/` | Postgres | 7 files |

The folder is the marker: `empty_tables` reads the path, so `tests/unit`
runs with no database at all.

| Technique | Used for |
|---|---|
| `httpx.MockTransport` | standing in for GitHub and the model |
| Separate test database | `pr_review_copilot_test`, truncated between tests |
| Generated RSA key | real signing, thrown away after the run |

`conftest.py` sets the environment before `src.config` is imported, and
refuses to run if `TEST_DATABASE_URL` and `DATABASE_URL` name the same
database — the suite would erase it.

Shared fakes live in `tests/fakes.py`. See [tests/README.md](../tests/README.md).

## 8. Known gaps

- **Deliveries that never arrive are invisible.** GitHub does not retry. If
  the service is asleep when a pull request opens, that review never happens
  and nothing records it.
- **Confidence thresholds are not calibrated** against real developer
  outcomes. The `outcomes` table collects the evidence; nothing reads it yet.
- **No concurrency tests.** Every test is single-threaded against fakes, so
  the failures that only appear under real concurrency are not reachable from
  the suite.
