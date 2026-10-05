# Low-Level Design

How the service is built, module by module. The high-level shape is in
[06_HLD.md](06_HLD.md); this document is the level below it — real signatures,
real types, real constraints.

**Status key used throughout:** `built` means the code exists and is tested.
`designed` means the interface is agreed but nothing is written yet.

---

## 1. Module map

```text
src/
  main.py            FastAPI application, startup checks, /health
  config.py          settings, read once and validated at startup
  pipeline.py        orchestrates one review; takes a PR, never a request

  webhooks/          receiving events from GitHub
    signature.py       HMAC-SHA256 verification
    events.py          which events matter, and the fields we keep
    router.py          POST /webhooks/github

  github/            talking to GitHub
    auth.py            App JWT, then installation token
    client.py          requests, pagination, headers

  diff/              working out what changed
    files.py           fetch the changed file list
    skip.py            which files are worth reviewing
    parse.py           patch text into hunks and line numbers

  db/                storage
    models.py          seven tables
    queries.py         reads and writes
    session.py         engine and sessions
    url.py             connection string translation

  context/           connected-file context          (designed)
  classification/    labelling the change type       (designed)
  review/            generating findings             (designed)
  judge/             verifying findings              (designed)
  filter/            confidence and deduplication    (designed)
  outcomes/          recording developer response    (designed)
  common/            shared helpers                  (empty)
```

### Dependency rules

Enforced by convention, not tooling. Violations are a design smell.

| Layer | May import | May not import |
|---|---|---|
| `config` | nothing from `src` | anything |
| `webhooks/signature` | nothing from `src` | anything, including FastAPI |
| `webhooks/events` | nothing from `src` | anything |
| `db/*` | `config`, `webhooks.events` | `github`, `diff`, `pipeline` |
| `github/*` | `config` | `db`, `diff`, `pipeline` |
| `diff/*` | `github.client` | `db`, `pipeline` |
| `pipeline` | everything below | `webhooks.router` |
| `webhooks/router` | `config`, `db`, `pipeline`, `webhooks.*` | `diff`, `github` directly |

Two rules carry weight. **`signature.py` imports no framework**, which is why
it can be tested without a web server. **`pipeline.py` never imports the
router**, which is why the evaluation harness can drive it directly.

---

## 2. Configuration — `src/config.py` · built

```python
@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    app_id: str
    client_id: str
    private_key_path: Path
    database_url: str

    def read_private_key(self) -> str: ...

@lru_cache(maxsize=1)
def get_settings() -> Settings: ...
```

| Variable | Purpose |
|---|---|
| `GITHUB_WEBHOOK_SECRET` | Shared secret for signature verification |
| `GITHUB_APP_ID` | Numeric App id, used in logs |
| `GITHUB_CLIENT_ID` | The `iss` claim in the App JWT |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the `.pem` |
| `DATABASE_URL` | PostgreSQL connection string |
| `TEST_DATABASE_URL` | Used only by the test suite |
| `NEON_DATABASE_URL` | Production database, becomes `DATABASE_URL` on deploy |

Missing variables are reported **by name** at startup. The key file is
checked for existence, not read, so a bad path fails immediately rather than
on the first webhook.

---

## 3. Webhook ingestion — `src/webhooks/` · built

### 3.1 `signature.py`

```python
SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="

def expected_signature(raw_body: bytes, secret: str) -> str
def is_valid_signature(raw_body: bytes, header_value: str | None, secret: str) -> bool
```

Two invariants:

1. **Hash the raw bytes.** `json.loads` then `json.dumps` produces semantically
   identical text with different bytes. The digest will not match.
2. **Compare with `hmac.compare_digest`.** A plain `==` returns early on the
   first differing byte, leaking how much of a guess was correct.

### 3.2 `events.py`

```python
PULL_REQUEST_EVENT = "pull_request"
ACTIONS_WE_REVIEW = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})

def is_actionable(event: str | None, action: str | None) -> bool

@dataclass(frozen=True)
class PullRequestEvent:
    delivery_id: str
    action: str
    installation_id: int
    repo_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str

    @classmethod
    def from_payload(cls, delivery_id: str, payload: dict) -> PullRequestEvent
```

`from_payload` raises `KeyError` on a missing field. The router converts that
into `WEBHOOK_PAYLOAD_INCOMPLETE` rather than letting it surface as a 500.

`repo_id` is GitHub's numeric id, not the name. Repositories get renamed; ids
do not.

### 3.3 `router.py`

```
POST /webhooks/github
```

| Header | Used for |
|---|---|
| `X-Hub-Signature-256` | Verification |
| `X-GitHub-Event` | Event type |
| `X-GitHub-Delivery` | Delivery-level idempotency key |

Seven steps, each returning immediately on failure:

| Step | On failure |
|---|---|
| 1. Verify signature | `401 WEBHOOK_SIGNATURE_INVALID`, nothing stored |
| 2. Read headers | `400 WEBHOOK_HEADERS_MISSING` |
| 3. Parse JSON | `400 WEBHOOK_PAYLOAD_MALFORMED` |
| 4. Extract fields, if actionable | `400 WEBHOOK_PAYLOAD_INCOMPLETE` |
| 5. Record the delivery | `200 duplicate` if already seen |
| 6. Claim the commit | `200 duplicate` if already claimed |
| 7. Schedule the pipeline | — |

**Every authenticated delivery returns 200**, including ignored ones and
duplicates. GitHub treats any non-2XX as a failed delivery, so a `409` on a
correctly handled duplicate would report correct behaviour as a failure.

Response bodies:

```json
{"status": "accepted",  "review_id": 1, "repository": "...", "pull_request": 7, "head_sha": "..."}
{"status": "duplicate", "delivery": "..."}
{"status": "ignored",   "event": "pull_request", "action": "labeled"}
{"error": {"code": "...", "message": "...", "requestId": "req_..."}}
```

---

## 4. GitHub access — `src/github/` · built

### 4.1 `auth.py`

```python
GITHUB_API = "https://api.github.com"
API_HEADERS = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2026-03-10"}

JWT_LIFETIME_SECONDS = 9 * 60
JWT_BACKDATE_SECONDS = 60
REFRESH_WHEN_UNDER = timedelta(minutes=5)

def build_app_jwt() -> str

@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime
    @property
    def is_still_usable(self) -> bool

async def fetch_installation_token(installation_id: int, *, client=None) -> InstallationToken
async def get_installation_token(installation_id: int, *, client=None) -> str
def clear_token_cache() -> None
```

Two-step authentication:

| Step | What | Lifetime | Scope |
|---|---|---|---|
| 1 | JWT signed RS256 with the App private key | 9 minutes | Proves identity only |
| 2 | Installation token from `POST /app/installations/{id}/access_tokens` | 1 hour | One installation's repositories |

JWT claims: `iat` backdated 60s for clock drift, `exp` under GitHub's 10-minute
ceiling, `iss` set to the **Client ID** (GitHub's current recommendation over
the App ID).

Tokens are cached per installation id and refetched when under five minutes
remain, so a request never begins with a token that expires mid-flight.

### 4.2 `client.py`

```python
class GitHubClient:
    def __init__(self, installation_id: int, *, client: httpx.AsyncClient | None = None)
    async def __aenter__(self) -> GitHubClient
    async def __aexit__(self, *exc_info) -> None
    async def get(self, path: str, **params) -> Any
    async def get_all_pages(self, path: str, *, per_page: int = 100) -> list[Any]
    async def post(self, path: str, json: Any) -> Any
```

`get_all_pages` follows `response.links["next"]["url"]` until exhausted. Query
parameters are dropped after the first request because the `next` link already
carries its own page number.

Header word differs by token type: `Bearer` for the App JWT, `token` for an
installation token.

The optional `client` argument exists so tests can inject
`httpx.MockTransport`. Nothing in the test suite reaches the network.

---

## 5. Diff ingestion — `src/diff/` · built

### 5.1 `files.py`

```python
GITHUB_FILE_LIMIT = 3000

@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None
    previous_filename: str | None = None

    @property
    def has_patch(self) -> bool
    @property
    def is_deleted(self) -> bool
    @property
    def is_renamed(self) -> bool
    @classmethod
    def from_api(cls, data: dict) -> ChangedFile

@dataclass(frozen=True)
class ChangedFileSet:
    files: list[ChangedFile]
    truncated: bool = False

    @property
    def reviewable(self) -> list[ChangedFile]
    @property
    def without_patch(self) -> list[ChangedFile]
    @property
    def total_changes(self) -> int

async def fetch_changed_files(github, repo_full_name: str, pr_number: int) -> ChangedFileSet
```

Source: `GET /repos/{owner}/{repo}/pulls/{n}/files`, 100 per page.

**GitHub caps the response at 3,000 files with no error.** Reaching exactly
that count sets `truncated` and logs a warning; without it a huge pull request
would be reviewed in part while appearing complete.

`patch` is absent for binary files and very large diffs. Those are separated
into `without_patch` rather than silently dropped, and kept distinct from
deleted files, where having no diff is normal.

### 5.2 `skip.py`

```python
MAX_CHANGED_LINES_PER_FILE = 500

@dataclass(frozen=True)
class SkipDecision:
    skip: bool
    reason: str | None = None

def decide(changed: ChangedFile) -> SkipDecision
def worth_reviewing(files: list[ChangedFile]) -> tuple[list[ChangedFile], dict[str, str]]
```

| Rule | Examples |
|---|---|
| Deleted | nothing to review |
| No diff | images, binaries |
| Lockfile | `package-lock.json`, `poetry.lock`, `go.sum`, `Cargo.lock` |
| Vendored or built | `node_modules/`, `vendor/`, `dist/`, `__snapshots__/` |
| Generated | `*.min.js`, `*.map`, `*_pb2.py`, `*.pb.go` |
| Oversized | over 500 changed lines in one file |

`worth_reviewing` returns the reason per skipped file, so logs say *why*
rather than a file silently vanishing.

### 5.3 `parse.py`

```python
ADDED, REMOVED, CONTEXT = "added", "removed", "context"
SIDE_OLD, SIDE_NEW = "LEFT", "RIGHT"

@dataclass(frozen=True)
class DiffLine:
    content: str
    kind: str
    old_line: int | None
    new_line: int | None

    @property
    def side(self) -> str
    @property
    def comment_line(self) -> int | None

@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    lines: list[DiffLine]

@dataclass(frozen=True)
class FileDiff:
    filename: str
    hunks: list[Hunk]
    parse_error: str | None = None

    @property
    def parsed(self) -> bool
    @property
    def added_line_numbers(self) -> list[int]
    @property
    def commentable_lines(self) -> set[int]
    def can_comment_on(self, line_number: int) -> bool

def ensure_headers(patch: str, filename: str, previous_filename: str | None = None) -> str
def parse_patch(patch: str, filename: str, previous_filename: str | None = None) -> FileDiff
def parse_changed_files(file_set: ChangedFileSet) -> list[FileDiff]
```

Three constraints encoded here:

**GitHub's `patch` field omits the `---`/`+++` headers** that `unidiff`
requires. `ensure_headers` adds them, using `previous_filename` on the old
side for renames.

**Both line numbers are kept per line.** GitHub deprecated the `position`
parameter for review comments in favour of `line` plus `side`. An added line
is `RIGHT` with its new number; a removed line is `LEFT` with its old number
and has no new number at all.

**`commentable_lines` is the posting constraint.** GitHub only accepts an
inline comment on a line present in the diff. A finding about line 300 of a
file whose diff covers 17–24 cannot be anchored there. This becomes load
bearing for connected-file context, where findings concern files that were
never changed.

`parse_patch` never raises. Malformed input returns a `FileDiff` with
`parse_error` set, so one bad file does not fail a review.

---

## 6. Storage — `src/db/` · built

### 6.1 Tables

| Table | Holds | Key constraint |
|---|---|---|
| `installations` | Who installed the App | `github_installation_id` unique |
| `repos` | Repositories we can read | `github_repo_id` unique |
| `webhook_events` | Every authenticated delivery | `delivery_id` unique |
| `reviews` | One commit on one pull request | `(repo_id, pr_number, head_sha)` unique |
| `findings` | One comment about one line | — |
| `outcomes` | What the developer did with it | — |
| `repo_suppressions` | Categories a repo has muted | `(repo_id, category)` |

Check constraints reject invalid `status`, `outcome` and `context_status`
values at the database level.

Indexes: `reviews(repo_id, created_at)` for the dashboard,
`reviews(status, updated_at)` for the stuck-review scan,
`findings(review_id)`, `outcomes(repo_id, category)`.

`webhook_events` stores identifying fields only, never the payload. The
payload contains source code, and retaining it would mean holding other
people's code without cause.

### 6.2 `queries.py`

```python
async def record_delivery(session, *, delivery_id, event, action, outcome,
                          pull_request: PullRequestEvent | None = None) -> int | None
async def update_delivery(session, delivery_row_id: int, **fields) -> None
async def ensure_repo(session, pull_request: PullRequestEvent) -> int
async def claim_review(session, repo_id: int, pull_request: PullRequestEvent) -> int | None
async def start_attempt(session, review_id: int) -> None
async def set_review_status(session, review_id: int, *, expected: str, new: str, **extra) -> bool
```

**Every write uses `INSERT ... ON CONFLICT DO NOTHING ... RETURNING id`.**
Read-then-write is unsafe: two simultaneous deliveries both see an empty table
and both insert. Returning `None` is how the caller learns it lost the race.

`set_review_status` only applies when the review is still in `expected`,
making the status change itself a lock.

### 6.3 `session.py` and `url.py`

`NullPool` — no pooling on our side. Neon's endpoint pools server-side, and a
pooled connection belongs to the event loop that opened it.

`normalise_database_url` rewrites `postgresql://` to `postgresql+asyncpg://`,
moves `sslmode` into `connect_args`, drops `channel_binding`, and sets
`statement_cache_size=0` when the host contains `-pooler`, because a
transaction pooler may route the next query to a different backend.

### 6.4 Migrations

Alembic, async template. `env.py` builds its engine with `create_async_engine`
directly rather than from `alembic.ini`, so hosted databases receive their SSL
`connect_args`. The URL is percent-escaped before entering the config parser.

---

## 7. Pipeline — `src/pipeline.py` · built through phase 2

```python
async def process_pull_request(event: PullRequestEvent, review_id: int, *, github_client=None) -> None
```

Takes a pull request and a review id. **Never an HTTP request.** This is what
lets the evaluation harness replay historical pull requests with no webhook.

Sequence:

1. Open its own session — the request's session is closed by the time a
   background task runs
2. `start_attempt`
3. `set_review_status(expected="received", new="processing")` — **this is the
   lock**; a `False` return means something else owns the work, and the run
   stops having made zero API calls
4. Fetch changed files
5. `worth_reviewing`, logging each skip with its reason
6. `parse_changed_files`
7. On no reviewable files → `skipped`
8. On any exception → `failed` with `error_code`

---

## 8. Idempotency

Two independent keys, catching different problems:

| Key | Catches | Typical cause |
|---|---|---|
| `webhook_events.delivery_id` | The same message twice | The Redeliver button |
| `reviews(repo_id, pr_number, head_sha)` | Two messages about the same code | Reopening a pull request with no new commits |

Both produce `200 duplicate`. When the second fires, the delivery row's
`outcome` is corrected from `accepted` to `duplicate`, so the record stays
honest about what happened.

Review ids skip numbers. A rejected insert still consumes a sequence value.
This is expected, not a lost row.

---

## 9. Review state machine

```
received ──> processing ──> posting ──> posted
   │             │                         
   │             ├──> skipped              
   │             ├──> no_findings          
   │             ├──> failed ──> processing (retry, under cap)
   │             └──> superseded           
   └──> superseded
```

| Status | Meaning |
|---|---|
| `received` | Commit claimed, nothing started |
| `processing` | Pipeline running |
| `posting` | Findings being sent to GitHub |
| `posted` | Comments live on the pull request |
| `skipped` | Nothing worth reviewing |
| `no_findings` | Reviewed, nothing to say |
| `superseded` | A newer commit made this review irrelevant |
| `failed` | `error_code` explains why |

`superseded` exists because two quick pushes overlap. The first review must
not post comments about lines that no longer exist.

### Ordering rules

1. A delivery is recorded before any work begins
2. A commit is claimed before the pipeline is scheduled
3. The status moves out of `received` before any external call
4. `attempts` increments before the status change, so a crash still counts
5. Findings are stored before posting, so a posting failure loses nothing
6. `github_review_id` is set only after GitHub accepts

---

## 10. Error handling

| Code | HTTP | Where | Stored |
|---|---|---|---|
| `WEBHOOK_SIGNATURE_INVALID` | 401 | router | no |
| `WEBHOOK_HEADERS_MISSING` | 400 | router | no |
| `WEBHOOK_PAYLOAD_MALFORMED` | 400 | router | no |
| `WEBHOOK_PAYLOAD_INCOMPLETE` | 400 | router | no |
| `PIPELINE_UNEXPECTED` | — | pipeline | `reviews.error_code` |
| `DIFF_FETCH_FAILED` | — | pipeline | `reviews.error_code` |

Nothing that fails verification is written. A pipeline failure degrades the
review, never the service.

Every error response carries a `requestId` for correlating logs.

---

## 11. Testing

107 tests. **No test reaches the network or the development database.**

| Technique | Used for |
|---|---|
| `httpx.MockTransport` | Standing in for GitHub |
| Separate test database | `pr_review_copilot_test`, truncated between tests |
| Generated RSA key | Real signing, thrown away after the run |
| Patched pipeline | Endpoint tests stay about the endpoint |

Shared fakes live in `tests/fakes.py`; environment setup and fixtures in
`tests/conftest.py`, which sets variables before `src.config` is imported.

---

## 12. Designed, not built

| Module | Planned interface |
|---|---|
| `context/` | `gather(repo, head_sha, diffs, budget) -> ContextBundle` |
| `classification/` | `classify(diffs) -> dict[str, ChangeKind]` |
| `review/` | `generate(diff, context, template) -> list[Finding]` |
| `judge/` | `verify(finding, context) -> Verdict` |
| `filter/` | `filter(findings, threshold) -> list[Finding]` |
| `outcomes/` | `record(comment_id, outcome) -> None` |

`context/` is the only one with experimental evidence: `spikes/context_spike.py`
achieves full recall on a fixture with two deliberate traps. Its confidence
scores are hand-chosen and uncalibrated.

---

## 13. Known gaps

1. **No recovery for interrupted reviews.** A crash during `processing` leaves
   the row there. The `(status, updated_at)` index exists for the scan that
   will fix this; the scan is unwritten.
2. **Deliveries that never arrive are invisible.** GitHub does not retry. If
   the service is asleep when a pull request opens, that review never happens
   and nothing records it.
3. **Confidence thresholds are guesses.** Nothing is calibrated against real
   outcomes yet.
4. **One language.** Every planned tree-sitter query assumes Python.
