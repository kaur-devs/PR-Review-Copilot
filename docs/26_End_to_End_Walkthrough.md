# End-to-End Walkthrough

Every part of the system in build order. For each part: what it does, how it
works, how it gets built, and what it is built with.

Status is marked per part — `BUILT`, `PROVEN` (validated in an experiment but
not in the service), or `PLANNED`.

---

## 0. What the product is

An AI code reviewer that installs as a GitHub App. You open a pull request and
it posts inline comments.

**The bet:** most AI reviewers only read the diff. A diff is not enough. If a
function changes from raising an error to returning nothing, the bug is not in
that function — it is in every caller that assumed a real value comes back.
Those callers are not in the diff. This project finds them.

**The constraint that shapes everything:** free-tier hosting and free-tier
language models. The scarce resource is requests per day, not money. So the
design optimises for *fewer model calls*, not fewer tokens.

---

## 1. Installation — the GitHub App · BUILT

**What it does.** Lets someone grant the service permission to read their code
and comment on their pull requests.

**How it works.** A GitHub App is one registration that many accounts can
install. Registering creates an App id, a client id, a private key, and one
webhook address. Installing grants that App access to chosen repositories and
creates an *installation*, identified by a number. That number is how the
service knows whose code it is looking at.

**How I built it.** Registered through GitHub's developer settings with three
permissions only: pull requests read and write, contents read, metadata read.
Subscribed to `pull_request` events. Generated a private key and stored it
outside version control.

**What I used.** GitHub's web interface. `smee.io` to forward events to my
laptop during development, because GitHub needs a public address.

---

## 2. Receiving events — the webhook endpoint · BUILT

**What it does.** Receives a message from GitHub every time a pull request is
opened or updated.

**How it works.** GitHub POSTs JSON to one address. It allows **ten seconds**
for a reply and **never retries** a failed delivery. So the endpoint does the
least possible work and replies, then does the real work afterwards.

**How I built it.** One FastAPI route, `POST /webhooks/github`. It reads the
raw request body, verifies it, records it, claims the work, schedules a
background task, and returns 200. Everything slow happens after the response
has gone.

**What I used.** `fastapi` for routing, `uvicorn` to run it, and FastAPI's
`BackgroundTasks` to defer the work.

---

## 3. Verifying the message is genuine · BUILT

**What it does.** Proves a message really came from GitHub and not from
someone who found the address.

**How it works.** GitHub and the service share a secret. GitHub mixes that
secret with the exact bytes of the message using HMAC-SHA256 and sends the
result in a header. The service does the same sum and compares. Only someone
holding the secret could produce a matching value.

**How I built it.** A single function over bytes, with no framework imports so
it can be tested without a server. Two details matter: it hashes the **raw
bytes**, because parsing the JSON and re-encoding it changes the bytes and
breaks the match; and it compares using `compare_digest`, which takes constant
time so nobody can guess the secret one character at a time.

**What I used.** Python's built-in `hmac` and `hashlib`.

---

## 4. Recording and not doing work twice · BUILT

**What it does.** Writes down every genuine message, and makes sure the same
commit is never reviewed twice.

**How it works.** Two separate protections:

| Check | Catches | Typical cause |
|---|---|---|
| Unique delivery id | The same message twice | Someone presses Redeliver |
| Unique repository + pull request + commit | Two messages about the same code | Reopening a pull request with no new commits |

Both return 200, because neither is an error. Returning an error code would
make GitHub mark correct behaviour as a failed delivery.

**How I built it.** The uniqueness is enforced by the **database**, not by
code. Every write says "insert this, and if it clashes with an existing row,
do nothing". Checking first and then writing looks correct but is not: two
messages arriving at the same instant would both find nothing and both write.

**What I used.** PostgreSQL, `sqlalchemy` to describe the tables, `asyncpg` as
the driver, `alembic` for versioned schema changes, and PostgreSQL's
`ON CONFLICT DO NOTHING`.

---

## 5. Proving who we are to GitHub · BUILT

**What it does.** Gets permission to read a specific customer's code.

**How it works.** Two steps.

1. Sign a short note with the App's private key saying "I am this App". Valid
   nine minutes. This proves identity but opens nothing.
2. Trade that note for a token scoped to one installation, valid one hour.
   Every real request uses this.

Two steps rather than one because the second token is narrow and short-lived.
If it leaks it expires within the hour and only ever touched one account. The
private key never leaves the server.

**How I built it.** Sign the note with RS256. Three claims: issued-at
backdated sixty seconds for clock drift, expiry under GitHub's ten-minute
ceiling, and the client id as issuer. POST it to GitHub's token endpoint.
Cache the returned token per installation and replace it when under five
minutes remain.

**What I used.** `pyjwt` with `cryptography` underneath for the signing,
`httpx` for the request.

---

## 6. Fetching what changed · BUILT

**What it does.** Asks GitHub which files the pull request touched, and gets
the diff for each.

**How it works.** One endpoint returns a list of changed files with their
diffs. Two things to handle: long lists arrive in pages, and GitHub caps the
response at **3,000 files with no error**, so a huge pull request silently
comes back incomplete.

**How I built it.** Follow the "next" link in the response headers until it
runs out. If exactly 3,000 files come back, mark the result truncated and warn.
Separate files with no diff — images and binaries — from deleted files, where
having no diff is normal.

**What I used.** `httpx`, and its built-in parsing of the `Link` header for
pagination.

---

## 7. Deciding which files are worth reading · BUILT

**What it does.** Throws away files nobody wants reviewed.

**How it works.** Six rules: deleted files, files with no diff, lockfiles,
vendored directories, machine-generated output, and anything over 500 changed
lines in a single file.

**Why it matters.** Every file kept costs model requests later, and the free
tier has a hard daily limit. Reviewing `package-lock.json` is pure waste.

**How I built it.** A function returning a decision plus a **reason**, so logs
say why a file was skipped rather than it silently vanishing.

**What I used.** Plain Python. No library needed.

---

## 8. Reading the diff · BUILT

**What it does.** Turns diff text into exact line numbers.

**How it works.** A diff is a text format with `@@` headers saying which lines
of the old and new file a block covers, then lines prefixed with `+`, `-` or a
space. Two counters walk forward to work out the real line numbers.

**The subtlety.** Every line needs **two** numbers — where it sits in the old
file and in the new one. This is because of how comments are posted: GitHub
deprecated the old positional system in favour of a line number plus a side.
An added line is on the right with its new number; a deleted line is on the
left with its old number and has no new number at all.

**The constraint.** GitHub only accepts an inline comment on a line that
appears in the diff. A finding about line 300 of a file whose diff covers
lines 17 to 24 cannot be posted there. The parser records which lines are
commentable, so this is known before trying.

**How I built it.** GitHub's diff text omits the `---` and `+++` header lines
that the parsing library requires, so those get added first. Malformed diffs
are reported as an error on that file rather than raising, so one bad file
does not fail the whole review.

**What I used.** `unidiff`.

---

## 9. Finding other affected files · PROVEN

**What it does.** Finds code elsewhere in the repository that depends on what
changed. This is the part that makes the project more than a prompt wrapper.

**How it works.** Five steps:

1. Download the repository at the exact commit being reviewed
2. Work out which *function or class* the changed lines sit inside
3. Search every file for that name — a plain text scan, no parsing
4. Parse only the files that matched, and confirm each hit is a real call
5. Rank the survivors, cut to a budget, and attach how each was found

**Why two search steps.** Parsing every file in a repository is too slow for
the free hosting tier, which gives a tenth of a CPU. Text scanning is nearly
free and over-returns; parsing thirty survivors is cheap and precise.

**What the experiment showed.** On a five-file test repository where the right
answer was known in advance, it found both genuinely affected files and
correctly rejected two that a text search would have wrongly included: one
that mentioned the name only in a comment, and one that called a *different*
function sharing the name. It also recovered a file that imported the function
under a different name, which text searching cannot do at all.

**One thing I had to guard against.** The parser never fails loudly. Feed it
broken code and it returns a normal-looking result rather than an error, so
the code has to ask explicitly whether parsing succeeded.

**How I will build it.** Port the experiment into the service, replacing the
local folder with a downloaded snapshot, and record how confident each result
is so the later stages can weigh it.

**What I use.** `tree-sitter` with the Python grammar. It parses source into a
tree and answers questions like "what function contains this line" and "is
this a real call or just a word in a comment". It cannot do type inference or
follow imports across files, which is why an import check is layered on top.

---

## 10. Labelling the kind of change · PLANNED

**What it does.** Tags each change so the reviewer asks the right questions.

**How it works.** A database migration, an API endpoint change, a dependency
bump and a logic tweak all deserve different review questions. Asking one
generic question about everything produces generic, ignorable comments.

**Why it goes before the expensive work.** Some changes need no surrounding
context at all. Knowing the kind first means skipping the costliest stage when
it would not help.

**How I will build it.** One model call handling **all files at once** rather
than one call per file. That is a deliberate choice: the free tier limits
requests per day, so batching cuts roughly twenty calls per review down to
about seven.

**What I will use.** A free-tier model provider behind an adapter, so
providers can be swapped when one runs out of quota.

---

## 11. Generating the review · PLANNED

**What it does.** Produces the actual findings.

**How it works.** The model receives the diff, the surrounding code found in
step 9, and a prompt template chosen by the label from step 10. It returns
structured findings: file, line, severity, category, and an explanation.

**Why structured rather than prose.** Prose cannot be filtered, deduplicated,
scored, or measured. Structure is what makes the rest of the pipeline
possible.

**How I will build it.** Separate prompt templates per change kind rather than
one general one. Request a fixed output shape and validate it, rejecting
anything malformed instead of trying to parse loose text.

**What I will use.** A free-tier model, with `pydantic` validating the shape
of what comes back.

---

## 12. Checking the findings · PLANNED

**What it does.** A second pass that throws away findings the code does not
actually support.

**Why it exists.** Language models produce confident, plausible, wrong
findings routinely. Posting those is how a reviewer becomes something people
mute.

**How it works.** Each finding goes back to a model with the relevant code and
one question: is this genuinely supported by what you see? Findings that
cannot be grounded are dropped before anyone sees them.

**How I will build it.** Batched, like the classifier, for the same quota
reason. Findings that arrived through weak evidence — a name match with no
import to back it up — are held to a higher bar.

**What I will use.** The same provider adapter.

---

## 13. Deciding what is worth posting · PLANNED

**What it does.** Drops weak and repeated findings.

**How it works.** Every finding carries a confidence score. Anything below a
threshold is stored but not posted. Duplicates — the same issue found twice,
or something already commented on in an earlier review of the same pull
request — are removed.

**Why store what is not posted.** Thresholds can then be tuned later by
querying the stored data, with no need to re-run any model. That turns tuning
from an expensive experiment into a database query.

**How I will build it.** A threshold plus a similarity check, both plain code.

**What I will use.** No library. SQL for the later tuning.

---

## 14. Posting the comments · PLANNED

**What it does.** Puts the findings on the pull request.

**How it works.** One batched review rather than a stream of individual
comments, so the developer gets a single notification instead of fifteen.
Each comment is anchored to a line number and a side.

**The complication.** A finding about a file that was *not changed* has nowhere
to sit, because comments must land on lines in the diff. Those need an anchor
line inside the diff, with the real evidence referenced in the comment text.

**How I will build it.** One API call creating a review with all comments
attached. Record GitHub's comment ids, which requires a follow-up call because
the creation response omits them. Those ids are what makes the next step
possible.

**What I will use.** `httpx` through the existing client.

---

## 15. Learning whether it helped · PLANNED

**What it does.** Records what developers did with each comment.

**Why it matters most.** This is what turns "I think it works" into a number,
and the project's whole claim is that it is measured rather than asserted.

**How it works.** GitHub sends an event when a review thread is resolved or
unresolved. Resolved suggests the comment was useful; dismissed suggests it
was not. Matching those to stored comment ids gives a real acceptance rate per
category.

**How I will build it.** Subscribe to the review thread event, match on the
stored comment id, and write an outcome row. Later, a repository that keeps
dismissing one category can have that category muted automatically.

**What I will use.** The existing webhook endpoint and one more table.

---

## 16. Measuring it properly · PLANNED

**What it does.** Replays old pull requests where the bugs are already known
and scores the output.

**Why the architecture allows it.** The pipeline takes a pull request and
nothing else. It never sees an HTTP request. That single decision is what lets
the harness drive it directly with no webhook, no signature, and no live
GitHub event. Without it the research half of the project could not exist.

**How it works.** Feed in historical pull requests with known-correct comments,
compare what the reviewer produces, and report precision and recall. Run the
pipeline with pieces switched off to show which pieces actually help.

**A measurement that costs nothing.** Whether the right file was retrieved in
step 9 can be scored with no model calls at all, just by checking whether the
file a later bugfix touched was in the context.

**How I will build it.** Use an existing public dataset of pull requests with
labelled comments rather than hand-curating one.

**What I will use.** `pytest` style runners and SQL for the scoring.

---

## 17. Running it for real · PLANNED

**What it does.** Puts the service somewhere GitHub can reach.

**How it works.** Free hosting sleeps after fifteen minutes idle and takes
about a minute to wake. GitHub waits ten seconds and never retries. So a pull
request opened while the service is asleep is **lost entirely** — a
correctness problem, not a slow one.

**How I will handle it.** A scheduled ping to keep it awake, and a scan that
finds pull requests with no review and picks them up.

**What I will use.** Render's free tier for the service, Neon's free
PostgreSQL for the database. Neon rather than Render's own free database,
because that one deletes itself after thirty days and the project runs eight
weeks.

---

## Summary of what is used

| Tool | For |
|---|---|
| `fastapi` + `uvicorn` | The web service |
| `hmac`, `hashlib` | Proving messages are genuine |
| `pyjwt` + `cryptography` | Proving who we are to GitHub |
| `httpx` | Talking to GitHub |
| `unidiff` | Reading diffs |
| `tree-sitter` | Understanding source code |
| `sqlalchemy` + `asyncpg` | The database |
| `alembic` | Schema changes |
| `pydantic` | Checking model output has the right shape |
| `pytest` | Tests |
| PostgreSQL, Neon, Render | Storage and hosting |
| A free-tier model provider | The review itself |

---

## Where it stands

**Working today:** installation, receiving events, verifying them, storing
them, not repeating work, authenticating to GitHub, fetching changed files,
filtering them, and reading the diffs. 107 tests, none of which touch the
network or the development database.

**Proven but not in the service:** finding other affected files.

**Not started:** labelling, generating, judging, filtering, posting, learning,
measuring, deploying.

**Known gaps, written down rather than hidden:** nothing recovers a review
interrupted halfway; a pull request opened while the service is asleep is
never seen; the confidence numbers are guesses not yet calibrated against real
outcomes; and everything assumes Python.
