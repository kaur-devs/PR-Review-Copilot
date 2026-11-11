# Agentic Architecture — End to End

How a pull request becomes a review, stage by stage, in the agent-based
design. Every stage names its components, what it costs, and how it differs
from the pipeline we have today.

Two names are used throughout:

- **NOW** — the linear pipeline in `src/`, working today.
- **NEW** — the agentic design.

Prices verified 11 November 2026 from Groq's model list and Anthropic's
pricing page. Token counts are modelled for a typical pull request of
300 changed lines across 6 files.


## The journey at a glance

```
  developer pushes
        │
        ▼
  ┌───────────────────────────────────────────────────────┐
  │ 0  GitHub          raises pull_request webhook        │  0 ms
  ├───────────────────────────────────────────────────────┤
  │ 1  INGRESS         verify, dedupe, enqueue, 202       │  <10 s budget
  ├───────────────────────────────────────────────────────┤
  │ 2  ORCHESTRATOR    claim the job, open a thread       │
  ├───────────────────────────────────────────────────────┤
  │ 3  FETCH + FILTER  changed files, skip rules          │  no LLM
  ├───────────────────────────────────────────────────────┤
  │ 4  PARSE           unified diff -> changed lines      │  no LLM
  ├───────────────────────────────────────────────────────┤
  │ 5  TOOLBOX         snapshot + tree-sitter index       │  no LLM
  ├───────────────────────────────────────────────────────┤
  │ 6  TRIAGE AGENT    what kind of PR, who should run    │  1 call
  ├───────────────────────────────────────────────────────┤
  │ 7  SPECIALISTS     2 agents, parallel, tool loops     │  6 calls
  ├───────────────────────────────────────────────────────┤
  │ 8  VERIFIER AGENT  selective, batched per file        │  7 calls
  ├───────────────────────────────────────────────────────┤
  │ 9  RANK + FILTER   dedupe, suppress, cap              │  no LLM
  ├───────────────────────────────────────────────────────┤
  │ 10 FIXER AGENT     one call per surviving finding     │  3 calls
  ├───────────────────────────────────────────────────────┤
  │ 11 SUMMARY AGENT   PR overview and risk map           │  1 call
  ├───────────────────────────────────────────────────────┤
  │ 12 PUBLISHER       one batched review to GitHub       │  no LLM
  ├───────────────────────────────────────────────────────┤
  │ 13 RECORD          findings, cost, feedback watch     │  no LLM
  └───────────────────────────────────────────────────────┘
```

Seven of fourteen stages involve no model at all. That is deliberate: the
model is used for judgement, never for bookkeeping.


## Stage 0 — The pull request is raised

A developer opens a PR, marks a draft ready, or pushes new commits to an
open PR. GitHub delivers a webhook to our endpoint.

| | |
|---|---|
| **Component** | GitHub App installation |
| **We use** | GitHub Apps, not OAuth and not an Action |
| **Permissions** | Pull requests: read/write · Contents: read · Metadata: read |
| **Events** | `pull_request` (opened, reopened, synchronize, ready_for_review) |
| **LLM calls** | none |

**Why a GitHub App, not an Action.** An Action runs inside the customer's
CI, charges their minutes, needs a workflow file committed to every repo,
and cannot hold cross-repo state. An App is installed once, owns its own
compute, and mints a short-lived installation token per repository.

**NOW vs NEW:** identical. Nothing about the trigger changes.


## Stage 1 — Ingress

GitHub gives us **ten seconds** to answer, and does **not retry** on
failure. So this stage does the minimum and returns.

| | |
|---|---|
| **Component** | `src/webhooks/router.py`, `signature.py`, `events.py` |
| **We use** | FastAPI |
| **LLM calls** | none |
| **Budget** | under 10 seconds, hard |

Steps:

1. **Verify** — HMAC-SHA256 over the raw request bytes against the webhook
   secret, compared in constant time. Done on raw bytes, not the parsed
   JSON, because re-serialising changes whitespace and breaks the digest.
2. **Deduplicate the delivery** — insert `webhook_events.delivery_id`. A
   unique constraint makes a replayed delivery a no-op.
3. **Deduplicate the work** — insert into `reviews` on
   `(repo_id, pr_number, head_sha)` with
   `ON CONFLICT DO NOTHING ... RETURNING id`. No returned id means this
   exact commit is already under review.
4. **Enqueue** and return `202 Accepted`.

**Two different idempotency keys, on purpose.** The delivery id stops
GitHub's own retries. The commit key stops two different deliveries from
reviewing the same code twice.

**NOW vs NEW:** unchanged. This is the most reliability-critical code we
have and the agentic redesign does not touch it.


## Stage 2 — Orchestrator claims the job

| | NOW | NEW |
|---|---|---|
| **Component** | `src/pipeline.py` | LangGraph `StateGraph` |
| **State lives in** | `reviews.status` column | LangGraph checkpoint + `reviews` |
| **Claiming** | conditional `UPDATE ... WHERE status='received'` | same, plus `thread_id` |
| **On crash** | `src/recovery.py` re-runs the whole review | resume at the last finished node |
| **LLM calls** | none | none |

The claim is a single conditional update: move `received -> processing`
only if the row is still `received`. If it returns zero rows, another
worker already has it, and we stop. That is how two workers consuming the
same message never both review.

**The change that matters.** Our `thread_id` is
`"{repo_id}:{pr_number}:{head_sha}"` — the idempotency key we already
have. LangGraph saves graph state against that thread after every node.

Today a crash means `recovery.py` finds the stranded row and starts again
from zero. In NOW that costs almost nothing. In NEW the specialist stage is 50,400
tokens, about half the run.

**State this in money, not tokens.** Resuming after the specialists saves
50,400 tokens, but in config A that is only **$0.0089** of a $0.10 run,
because the money is in the Sonnet verifier. The crash that actually
hurts is one *during* verification: losing one completed per-file loop
costs **$0.0456**, five times more.

So if checkpoints are kept, **each per-file verifier loop must be its own
checkpointed step.** A single checkpoint after the specialists protects
the cheap part and leaves the expensive part exposed.

`recovery.py` stays, with a smaller job: it still scans for abandoned
reviews on startup, still resumes at most 5, still does not block the
webhook listener. It just calls `graph.ainvoke(thread_id)` instead of
re-running the pipeline.


## Stage 3 — Fetch and filter

| | |
|---|---|
| **Component** | `src/diff/files.py`, `src/diff/skip.py` |
| **We use** | GitHub REST, installation token |
| **LLM calls** | none |

`GET /repos/{owner}/{repo}/pulls/{n}/files` returns changed files with
patches. GitHub truncates at 3,000 files; we record that.

Then `worth_reviewing()` drops files by deterministic rules, in order:

| Rule | Reason given |
|---|---|
| Vendored path | `node_modules/`, `vendor/`, `third_party/` |
| Generated | lockfiles, `*.pb.go`, minified bundles |
| Unsupported language | "we cannot parse this language" |
| Too large | over 500 changed lines in one file |
| Deleted | nothing to review |

Order matters: a vendored `.js` file reports "vendored", not "unparseable",
because that is the more useful reason.

**Why rules and not a model.** This stage decides what we pay to look at.
Paying a model to decide what to pay for is circular. It is also the
cheapest possible precision win — a `package-lock.json` can be 40,000
tokens of pure noise.

**NOW vs NEW:** unchanged. The supported-language whitelist now covers
Python, JavaScript, TypeScript and TSX.


## Stage 4 — Parse the diff

| | |
|---|---|
| **Component** | `src/diff/parse.py` |
| **We use** | hand-written unified-diff parser |
| **LLM calls** | none |

Hunk headers (`@@ -a,b +c,d @@`) map each added line to its line number in
the head commit. That mapping is what lets a comment anchor to the right
line later. A file that fails to parse is recorded in `problems` and
skipped — never guessed at.

**NOW vs NEW:** unchanged.


## Stage 5 — The toolbox (biggest structural change)

| | NOW | NEW |
|---|---|---|
| **Component** | `src/context/` | same code, re-exposed as tools |
| **We use** | tree-sitter, tarball snapshot | identical |
| **Called by** | `gather_context()`, once, before the model | the model, on demand |
| **LLM calls** | none | none (the tools themselves) |

**NOW — context is pre-computed.** `gather_context()` runs *before* any
model sees anything:

1. Download the repo tarball at `head_sha`, stream to disk (50 MB cap).
2. `changed_symbols()` — parse each changed file, find which function or
   class each changed line sits in.
3. `find_references()` — two passes. A byte scan for the symbol's name
   across the snapshot, then AST verification of each hit, scored:

   | Relation | Confidence |
   |---|---|
   | call with a confirmed import | 0.90 |
   | call through the module name | 0.90 |
   | passed without being called, imported | 0.70 |
   | call with no import found | 0.50 |
   | attribute on some other object | 0.25 |
   | name appears in a comment or string | 0.10 |

4. `assemble()` — cut ≤60-line windows around each reference, centred on
   the use, within a 12,000-character budget.

**The ceiling.** Step 3 searches for *the symbol's name*. Anything whose
link to the change is not that identifier is invisible: a renamed dict key,
a column referenced in raw SQL, a dynamically dispatched handler, an
exception type that is no longer raised. And the whole thing runs before we
know what the bug is, so it cannot follow a hunch — there is no hunch yet.

**NEW — the same functions become callable tools.**

```
read_file(path, start, end)      -> Snapshot.read, windowed
grep(pattern, glob)              -> byte scan across the snapshot
find_references(symbol)          -> references.find_references
get_definition(symbol)           -> symbols.symbol_at_line
list_tests(path)                 -> path convention + import graph
```

Nothing is thrown away. The same code runs; the caller changes from our
orchestrator to the model.

**The honest trade.** The confidence table above is *calibrated* — 0.9
means an import edge was verified to exist. An agent returns "I checked and
it is real", which is an assertion, not a measurement, and `gate.py`'s
0.6 floor stops meaning anything. Mitigation: keep `find_references` as a
tool so its numeric confidence still reaches the model as evidence, and
require the agent to cite tool output in every finding.


## Stage 6 — Triage agent

| | NOW | NEW |
|---|---|---|
| **Component** | `src/classification/` | triage node |
| **Job** | label each file: logic / config / test / docs | label the PR, choose specialists |
| **Scope** | per file | whole PR |
| **Loop** | no | no — single call |
| **Model** | `gpt-oss-20b` (cheap) | `gpt-oss-20b` |
| **Tokens** | 6,000 in / 600 out | 2,500 in / 300 out |

NOW classifies files so `generate()` can treat a config change differently
from a logic change. Rules handle the obvious cases (`*.md` is docs) and
only the ambiguous ones reach the model, batched to a 3,000-token limit per
request — a fix for a real bug found on a 65-file PR where one oversized
prompt silently truncated.

NEW keeps that and adds routing: reading only file names, line counts and
the PR title, it answers *which specialists should wake up*.

**This stage controls the entire bill.** A docs-only PR wakes nobody and
costs $0.0003. Without triage, every PR pays for both specialists
($0.0089) whatever it contains.


## Stage 7 — Specialist reviewer agents

| | NOW | NEW |
|---|---|---|
| **Component** | `src/review/generate.py` | 2 parallel agent nodes |
| **Prompts** | one, broad | two, narrowed |
| **Context** | pre-built, fixed | **pre-built as turn zero**, then tools |
| **Calls** | 1 | ~6 (2 agents x ~3 turns) |
| **Tokens** | 5,500 in / 1,500 out | 50,400 in / 1,800 out |
| **Model** | `gpt-oss-120b` | `gpt-oss-120b` |
| **Cost** | $0.0017 | $0.0086 |

Two agents, not four. Narrow prompts beat broad ones, but there is no
evidence the gain keeps compounding past two:

- **Is this code wrong** — correctness and security
- **Is this code robust** — error handling and concurrency

Test coverage is **not** an agent. "Does a test touch this changed
function?" is a path-convention and import-graph lookup, machinery that
already exists in `src/context/`. It runs deterministically and feeds a
fact into the prompt ("no test file imports this module") rather than
being something an agent reasons about.

### Turn zero: the pre-computed context stays

The agent does not start cold. Its first prompt already contains what
`gather_context()` produces today: the changed symbols, their verified
references, and the confidence table.

This matters for two reasons.

**It cuts turns.** Quadratic growth means a bigger first prompt paid once
beats four more turns that each re-send everything:

| | Turn-1 input | Turns | Total |
|---|---|---|---|
| Cold start | 2,800 | 8 | 47,600 |
| With turn zero | 5,800 | 4 | 28,600 |

**It rescues calibrated confidence.** The main cost of going agentic was
losing the 0.9 / 0.7 / 0.5 / 0.25 / 0.1 scoring to an agent asserting "I
checked and it is real". If the agent *starts* with that table, the
numbers survive as evidence and `gate.py`'s 0.6 floor still means
something.

### Parallel tool calls

The model may request several tools in one turn. Because the saving comes
from re-sending the prefix fewer times, this roughly halves the stage
again:

| | Model calls | Total input |
|---|---|---|
| Serial: 3 lookups over 3 turns | 4 | 28,600 |
| Parallel: 3 lookups in 1 turn | 2 | 14,500 |

Parallel tool calling is a model capability, not a prompt trick. Verify it
empirically on `gpt-oss-120b`; if it emits one call per turn, the loop
degrades to serial with no loss of correctness, only of cost.

### Early exit

Stop on **no new information**, not on "no new candidates". Agents gather
for several turns and emit findings at the end, so a candidate-based rule
would fire on turn 2 of every run, before anything had been found.

The signals that actually mean a loop is stuck:

- the model requests a file or symbol it already has in context
- a tool returns a result identical to one already in the conversation
- the model emits findings, or says nothing was found (terminal anyway)

Implementation: hash each tool call's `(name, args)` and stop on a repeat.

### Merging parallel writes

Both agents write to one `candidates` list, which needs a reducer:

```python
candidates: Annotated[list, operator.add]
```

Without it the two writes overwrite each other. Each specialist is capped
at `MAX_CANDIDATES_PER_SPECIALIST = 4`, ranked by severity x confidence,
so a misfiring agent cannot flood the expensive verification stage.

## Stage 8 — Verifier agent

| | NOW | NEW |
|---|---|---|
| **Component** | `src/judge/verify.py` | verifier node, with tools |
| **Sees** | the same context the generator had | can go and look |
| **Loop** | no | yes, one loop **per file** |
| **Scope** | every finding | **selective** |
| **Calls** | 1 | ~7 |
| **Tokens** | 5,000 in / 800 out | 41,600 in / 2,600 out |
| **Model** | `qwen3.8-27b` | **Sonnet 5.5** (agentic) + `gpt-oss-120b` (cheap) |
| **Cost** | $0.0072 | $0.0924 |

A second model, from a different family, decides each finding is
`supported`, `contradicted`, or `unverifiable`. Contradicted findings are
dropped. Unverifiable ones are kept but multiplied by 0.7:

```python
UNVERIFIABLE_PENALTY = 0.7
```

**That constant is an admission.** It exists because the judge wanted to
check something and had no way to check it. An agentic verifier *can*
check, so the unverifiable bucket shrinks toward zero.

### Selective routing

Verification carries the premium model, so not every candidate earns it:

| Candidate | Verifier |
|---|---|
| High severity, **or** confidence below 0.75 | agentic loop, Sonnet 5.5 |
| Everything else | the existing single-call judge |

### Batched per file, not per candidate

Candidates cluster in the files the PR touched, and their context overlaps
heavily. One loop per *file* loads that file once and judges every
candidate in it together, instead of reloading it per candidate.

### Why this is the right place for a loop

The verifier arrives with a **specific hypothesis** to test, so the loop
converges in three or four turns instead of wandering. A generator asked
"find bugs" has no natural stopping point; a verifier asked "is this
real?" does.

After the revisions, verification is **88% of the per-PR bill**. That is
intentional and it is the healthy shape: the exploratory stage is cheap,
and the money sits on the stage that decides precision, which is the
metric the product is judged on. It is also where the next tuning lever
is — move the 0.75 threshold and the bill moves with it.

## Stage 9 — Rank and filter

| | |
|---|---|
| **Component** | `src/filter/gate.py`, `src/filter/repeat.py` |
| **LLM calls** | **none, deliberately** |
| **Cost** | $0 |

Three deterministic steps:

1. **Gate** — drop anything under 0.6 confidence or below medium severity.
2. **Repeats** — in NOW this runs here, after the gate. **In NEW it moves
   to immediately after stage 7**, before verification, so a re-push does
   not pay to verify findings it has already said. Matched on
   `(file, category, line +/- 3)`, **not** message text, because the
   generator is non-deterministic even at temperature 0.
3. **Cap** — at most 10 inline comments, ranked by severity x confidence.

**Why no model here.** When someone asks "how do you stop it being noisy?",
the answer should be a hard cap in code, not a model's promise.

**The severity gate also moves before verification.** Verifying a
low-severity candidate that the gate will drop anyway is pure waste. Only
the confidence check stays here, because verification is what sets the
final confidence.

Repeats still compare against findings that were actually *posted*, not
merely generated: one held back last time was never seen by anyone, so
saying it now is new rather than a repeat.

**NOW vs NEW:** unchanged. This is the noise control and it stays
deterministic in both designs.


## Stage 10 — Fixer agent

| | NOW | NEW |
|---|---|---|
| **Component** | `src/filter/suggest.py` | same + a model call |
| **Produces** | GitHub ```` ```suggestion ```` block | same |
| **Calls** | 0 (the generator already wrote it) | **one per surviving finding** |
| **Tokens** | 0 | 2,000 in / 400 out, each |

The call count here is not fixed. In the worked example two findings
survive the gate, so the fixer runs twice. A clean PR does not run it at
all.

A suggestion block **replaces the anchored line entirely**, which is why
inserting a line means including the original one. Three guards run before
any suggestion is offered:

1. `realign()` — shift the block to the file's real indentation, by one
   amount, preserving internal shape. Refuses on mixed tabs and spaces.
2. `is_valid_source()` — Python uses `compile()`; JS/TS use tree-sitter.
   **Weaker outside Python**, and that limitation is written in the code:
   tree-sitter is deliberately tolerant and accepts de-indented code
   CPython rejects.
3. `keeps_its_place()` — a line that compiles but has escaped its `if`
   block silently changes behaviour. Caught by comparing block depth.

**Level 2, not level 3.** We offer a suggestion with an Apply button. We do
not commit to the user's branch. That is the answer to "can you push fixed
code when production breaks": we could, and we chose not to, because a bot
with write access to source is a different risk product.


## Stage 11 — Summary agent

| | NOW | NEW |
|---|---|---|
| **Exists** | **no** | yes |
| **Calls** | 0 | 1 |
| **Tokens** | 0 | 3,000 in / 600 out |

A single top-level comment, updated in place on each push: three sentences
on what changed, a risk map ("touches auth middleware", "adds a migration",
"no tests for the new retry path"), and what was reviewed versus skipped.

This is a genuine gap in NOW. Both reference PRDs rank it P0, and a
reviewer opening our PR today sees inline comments with no orientation.


## Stage 12 — Publish

| | |
|---|---|
| **Component** | `src/github/review.py`, `client.py`, `auth.py` |
| **We use** | GitHub Reviews API, one batched review |
| **LLM calls** | none |

Authentication is two-step: sign an RS256 JWT with the app private key
(9-minute life, `iss` = client id), exchange it for an installation token
(1 hour, opaque `ghs_`, sent as `Authorization: token`). The JWT identifies
the *app*; the installation token identifies the app *acting on one
repository*.

All comments go in **one** review, not N individual comments — one
notification for the author instead of ten, and one API call against the
rate limit. Anchors are re-validated immediately before posting; a comment
whose line has vanished falls back to file level rather than failing.

**NOW vs NEW:** unchanged, plus the summary comment.


## Stage 13 — Record and watch

| | |
|---|---|
| **Component** | `src/db/queries.py`, `src/outcomes/` |
| **LLM calls** | none |

Store findings with their GitHub comment ids, match posted comments back to
finding rows, move the review to `posted`, record token and cost
accounting. Then watch for reactions, resolved threads and replies, which
feed the acceptance-rate metric and the suppression list.


## Two configurations

Every cost figure below belongs to one of these. They are never mixed.

| | **B — Free-tier** (recommended) | **A — Premium verifier** |
|---|---|---|
| Triage | `gpt-oss-20b` | `gpt-oss-20b` |
| Specialists | `gpt-oss-120b` | `gpt-oss-120b` |
| **Agentic verifier** | **`qwen3.8-27b`** | Claude Sonnet 5.5 |
| Cheap verifier | `gpt-oss-120b` | `gpt-oss-120b` |
| Fixer, summary | `gpt-oss-120b` | `gpt-oss-120b` |
| Runs on Groq free tier | **yes** | no — Anthropic has no free API tier |
| Per PR with findings | **$0.0491** | $0.1038 |

### Why the verifier is never the generator's own model

`src/judge/verify.py` already states the rule:

> *A different model from the one that generated them, because a model
> asked to check its own work mostly agrees with itself.*

`qwen3.8-27b` is a different family from `gpt-oss-120b`, is already this
project's `DEFAULT_JUDGE_MODEL`, and is on Groq's free tier. Running the
verifier on `gpt-oss-120b` would be cheaper ($0.0191 per PR) but would
have the generator marking its own homework. **That saving is not
available; it is the principle being dropped.**

Config A exists only as a measured upgrade path. Do not reach for it
until the eval harness shows `qwen3.8-27b` is not good enough.

### Development vs production

Identical code and identical models. Only the account changes.

| | Development | Production |
|---|---|---|
| Models | config B | config B |
| Groq account | free tier | pay-as-you-go |
| Cost | **$0** | $0.0491 per PR with findings |
| Limits | 8K TPM, 200K TPD (~2 reviews/day) | 250K TPM |

There is no code branch between the two — `src/config.py` already reads
the models and key from the environment.


## Cost, assembled

Per pull request, 300 changed lines, 6 files.

**v1** is the first design: four specialists, cold start, one verification
loop per candidate. **v2** applies six revisions — two specialists,
pre-computed context as turn zero, parallel tool calls, early exit on
no-new-information, selective verification, verification batched per file.

### Token totals

| Stage | NOW | NEW v1 | **NEW v2** |
|---|---|---|---|
| Triage | 6,000 / 600 | 2,500 / 300 | 2,500 / 300 |
| Specialists | 5,500 / 1,500 | 190,400 / 7,200 | **50,400 / 1,800** |
| Verifier | 5,000 / 800 | 68,400 / 4,800 | **41,600 / 2,600** |
| Fixer | 0 / 0 | 6,000 / 1,200 | 4,000 / 800 |
| Summary | 0 / 0 | 3,000 / 600 | 3,000 / 600 |
| **Total in** | **16,500** | **270,300** | **101,500** |
| **Total out** | **2,900** | **14,100** | **6,100** |

v2 is 62% cheaper in tokens than v1, and 6.2x NOW rather than 16x.

### Cost per scenario

Call counts and costs are computed from one model; see
`## Call volume in practice` for where the scenario shares come from.

| Scenario | Share | Calls | **A** $/PR | **B** $/PR |
|---|---|---|---|---|
| Docs / lockfile only | 12% | 0 | $0.0000 | $0.0000 |
| Config only (triage stops it) | 6% | 1 | $0.0003 | $0.0003 |
| Clean PR, no findings | 34% | 8 | $0.0097 | $0.0097 |
| Re-push, all repeats | 8% | 7 | $0.0089 | $0.0089 |
| **PR with 3 findings** | 32% | **18** | **$0.1038** | **$0.0491** |
| Large / complex PR | 8% | **24** | **$0.2546** | **$0.1179** |
| **Weighted average** | | **11.0** | **$0.0576** | **$0.0292** |

The large-PR row is modelled explicitly rather than guessed: two agents
at the 6-turn cap on a bigger diff is 144,000 input tokens, and eight
candidates across five files is five verifier loops at 91,500 — 235,500
in total, 2.3x the 18-call case.

| | **B — Free-tier** | A — Premium | **NOW** |
|---|---|---|---|
| Headline (PR with findings) | **$0.0491** | $0.1038 | $0.0102 |
| Expected per PR | **$0.0292** | $0.0576 | $0.0065 |
| 100 PRs / month | **$2.92** | $5.76 | $0.65 |

**B is 4.5x NOW** — and $0 during development, because every model in it
is on Groq's free tier. A is 8.9x NOW and needs a paid Anthropic key.

**NOW is the pipeline running in `src/` today**, not a hypothetical
baseline: `src/classification/` then `src/review/generate.py` then
`src/judge/verify.py`, 3-4 calls. Its costs use the models the code
actually names — `gpt-oss-120b` for classification and generation,
**`qwen3.8-27b`** for the judge (`DEFAULT_JUDGE_MODEL`), which is
$0.80/$4.00 rather than $0.15/$0.60.

| NOW stage | Model | Tokens | Cost | Share |
|---|---|---|---|---|
| Classify | `gpt-oss-120b` | 6,000 / 600 | $0.00126 | 12% |
| Generate | `gpt-oss-120b` | 5,500 / 1,500 | $0.00172 | 17% |
| **Judge** | **`qwen3.8-27b`** | 5,000 / 800 | **$0.00720** | **71%** |
| | | | **$0.01018** | |

A PR with no findings skips the judge entirely and costs $0.00298.

**The verification stage already dominates the bill.** NOW spends 71% on
the judge; config A spends 88% on the verifier. The agentic design does
not introduce that trade-off, it continues one already made: a second
opinion from a different model family is what holds precision up. The
change is that the verifier can also go and look.

**Quote the expected value, not the headline.** **60%** of pull requests
(docs, config, clean, re-push) never reach the verifier, which is 88% of
config A's bill on a PR that does.

### Where the money sits (config A, PR with findings)

| Stage | Cost | Share |
|---|---|---|
| Triage | $0.0003 | 0.3% |
| Specialists | $0.0086 | 8.3% |
| **Selective verifier (Sonnet)** | **$0.0912** | **88.4%** |
| Cheap verifier | $0.0012 | 1.2% |
| Fixer + summary | $0.0019 | 1.8% |

The shape is intentional. Exploration is cheap; the money sits on the
stage that decides precision. The 0.75 confidence threshold that routes
candidates to the agentic verifier is therefore the main cost dial.

### Prompt caching

Agent loops re-send their prefix every turn, which makes them a good
caching workload. A cache hit costs 10% of base input, 5% on Sonnet 5.5
and Opus 5.5.

By this document's own turn sizes (4,300 / 6,300 / 7,700) the re-read
prefix is **61% of input, not 80%**. Cache writes cost 1.25x base, and a
3-turn loop reads each segment at most twice, so the saving on config A's
verifier line is **roughly 37-45%**, not half.


## Call volume in practice

18 calls is the case **with findings**, not a flat rate. Every expensive
stage is conditional.

| Stage | Calls | Skipped entirely when |
|---|---|---|
| 3 Skip rules | 0 | — (always runs, can end the review) |
| 6 Triage | 1 | no reviewable files survived stage 3 |
| 7 Specialists | 0-12 | triage says run nobody; turns vary 2-6 |
| **8 Verifier** | **0-14** | **no candidates were found** |
| 10 Fixer | 0-10 | nothing survived the gate; one call per finding |
| 11 Summary | 0-1 | re-push where nothing is new |

```
calls per PR
 0 ████████████                      18%   docs + config
 7 ████████                           8%   repeats
 8 ██████████████████████████████    34%   clean
18 ████████████████████████████      32%   findings
24 ████████                           8%   large
       ▲
       └── expected value 11.0
```

The **share** column is an assumption about a repository's traffic mix,
not a measurement. The calls and costs are computed. Repositories doing
mostly feature work shift weight toward the 17-call row.

| Number | Use it for |
|---|---|
| 0 calls | floor — what the skip rules buy |
| **11.0 calls, $0.0292 (B)** | **the honest expected cost per PR** |
| 18 calls, $0.0491 (B) | a worked example, a PR *with* findings |
| 38 calls | hard ceiling from the caps; capacity planning only |

### Is this many calls normal?

By published reference points, this is a small system — but **call count
is not only a cost question**, and token totals do not settle it.

| | 1 call | 18 calls |
|---|---|---|
| Token cost | same | same |
| **Latency** | 1 round trip | **8 serial round trips on the critical path** |
| **Failure surface** | 1 chance to fail | **18**, each needing retry logic |
| **Debuggability** | one request to inspect | a trajectory to reconstruct |
| **RPM budget** | 1 | 17 against a requests-per-minute limit |
| Non-determinism | one sample | compounds across turns |

For scale only:

| System | Turns |
|---|---|
| SWE-agent (GPT-4), full SWE-bench | ~15 for **one** bug fix |
| 2026 agent workload study, ReAct-style | 12-62 per task |
| Research harness caps | 50-500 |
| **This system** | **~11 average, 18 with findings** |

So the volume is modest. The reason to keep it modest is latency and
failure surface, not the bill.


## Caps — part of the loop's definition

An average of 10 means nothing without a ceiling. Agent loops have long
tails; research harnesses cap at 50-500 for exactly this reason. These
are not a later hardening pass, they belong in the first version of the
loop alongside the stop condition.

| Cap | Value | Why |
|---|---|---|
| `MAX_TURNS_PER_AGENT` | 6 | 2x the expected 3. Hard stop |
| `MAX_TOOL_CALLS_PER_TURN` | 5 | stops a model requesting 40 greps at once |
| `MAX_CANDIDATES_PER_SPECIALIST` | 4 | stops a noisy agent flooding verification |
| `MAX_TOKENS_SPECIALISTS` | 160,000 | **per stage, not per review** |
| `MAX_TOKENS_VERIFIER` | 120,000 | the expensive stage gets its own budget |
| Repeat detection | hash `(tool, args)` | the early-exit signal; catches a stuck loop |

With these, 11 is the average and ~38 the worst case. Without them the
worst case is unbounded.

**A single `MAX_TOKENS_PER_REVIEW = 150,000` does not work.** On a large
PR the specialists alone reach 144,000, so a per-review cap trips before
verification starts and the expensive, precision-deciding stage never
runs. Budget each stage separately.


## Latency

Latency follows **loop depth**, not call count. Most of the 18 calls run
in parallel; the critical path is **8 serial model calls**.

```
  fetch + snapshot + index ─────────────── 4.0s
  triage ──────────────────────────────── 0.8s   call 1
                 ┌─ agent A: t1 → t2 → t3 ┐
  specialists ───┤                        ├───── 5.1s   calls 2-7
                 └─ agent B: t1 → t2 → t3 ┘      (3 deep, not 6)
                 ┌─ loop 1: t1 → t2 → t3 ┐
  verifier ──────┤  loop 2: t1 → t2 → t3 ├────── 15.7s  calls 8-14
                 └─ cheap judge (hidden)  ┘      (3 deep, not 7)
                 ┌─ fixer x2  ┐
  tail ──────────┤            ├────────────────── 1.8s   calls 15-17
                 └─ summary   ┘
  publish ──────────────────────────────── 1.0s
                                          ─────
                                           28.3s
```

### Compute-bound (adequate rate limit)

| Segment | Seconds | Share |
|---|---|---|
| Fetch + snapshot + index | 4.0 | 14% |
| Triage | 0.8 | 3% |
| Specialists | 5.1 | 18% |
| **Verifier (config A)** | **15.7** | **55%** |
| Fixer + summary | 1.8 | 6% |
| Publish | 1.0 | 4% |
| **v2 total** | **28.3s** | |
| NOW, for comparison | 14.4s | |

28 seconds against a 120-second target. Modelled with Groq decode speeds
from their published model list (500 tok/s for `gpt-oss-120b`) and Sonnet
5.5 at 100 tok/s with 1.0s TTFT; reported Sonnet figures range 88-150
tok/s and 0.4-1.6s, so treat the verifier line as +/-30%.

### Config A costs 88% of the money and 55% of the time

Both for the same reason: Sonnet is roughly 5x slower per output token
than Groq's hardware and 13x the price.

| Verifier | Total latency | Verifier cost |
|---|---|---|
| **A** — Sonnet 5.5 | 28.3s | $0.0912 |
| **B** — `gpt-oss-120b` | **17.2s** (-39%) | **$0.0012** |

Precision, cost and latency converge on one decision. The eval harness
settles it.

### Rate-limit-bound — the real constraint

Groq's limits are **per model ID**, each with its own bucket. Confirmed
from console.groq.com/docs/rate-limits, both plan tabs, 11 Nov 2026.

| Model | Free TPM | Free TPD | Dev TPM | Dev TPD |
|---|---|---|---|---|
| `openai/gpt-oss-20b` | 8K | 200K | 250K | none |
| `openai/gpt-oss-120b` | 8K | 200K | 250K | none |
| `qwen/qwen3.8-27b` | 8K | 200K | 250K | none |

RPM is 30 free / 1K developer, RPD 1K / 500K. Neither ever binds: at 18
calls a review, 1,000 requests a day is 55 reviews. **Tokens are the only
constraint.**

Config B's load, split by bucket:

| Model | Tokens/review | Free: time | Free: reviews/day |
|---|---|---|---|
| `gpt-oss-20b` | 2,800 | 21s | 71 |
| **`gpt-oss-120b`** | **68,800** | **8.6 min** | **2.9** |
| `qwen3.8-27b` | 38,400 | 4.8 min | 5.2 |

| Plan | Time per review | Reviews/day |
|---|---|---|
| **Free** | **13.1 min** | **2.9** |
| **Developer** | **30s** | **unlimited** |

The daily cap matters more than the per-minute rate. On the Developer
plan TPD is removed entirely; that is the real unlock, not the 31x TPM.

**Free-tier optimisation: spread the load.** `gpt-oss-120b` carries both
specialists plus the fixer and summary, which is why it is the
bottleneck. Moving one specialist to `gpt-oss-20b` cuts its load to
~42,700 and lifts capacity from **2.9 to 4.7 reviews/day** at no cost —
one environment variable. The trade is a weaker model on one of the two
review prompts, which the eval harness can price.

Even on the Developer plan the review is not fully compute-bound: the
specialists' 52,200 tokens need 12.5s of rate budget against 5.1s of
compute, which is why the total is 30s rather than the 17.2s the pure
compute model predicts.

`src/review/provider.py` reads `x-ratelimit-remaining-tokens` and
`x-ratelimit-reset-tokens` and waits before hitting a 429 rather than
retrying after one. That file stays ours in NEW because `ChatGroq` does
not do this.

### Two latency measures worth building

**Progressive posting.** Post the summary comment as soon as it exists,
then update it with inline findings:

```
t = 6s    summary comment appears      <- author sees activity
t = 28s   updated, inline comments added
```

Perceived latency becomes 6 seconds. On the free tier it is the
difference between "is this broken" and "it is working on it".

**Cancel superseded runs.** A new commit while a review is in flight
should kill the older run. The `head_sha` key already identifies it. A
stale review burning 13 minutes of free-tier budget is worse than no
review.


## Scenario walkthroughs

### Case 1 — PR with findings (18 calls, $0.0491 config B)

```
[0-5]  no model.  4 files -> 2 reviewable (lockfile generated, README
       unparseable).  changed_symbols: streamChat, retrieve_context.
       find_references: useChat.jsx 0.90, chat.py 0.90.          4.0s

[6]    CALL 1   triage -> {api_change, high, run both}           0.8s

[7]    Agent A "is this code wrong"
       CALL 2   turn-zero prompt -> 3 parallel tool calls
       CALL 3   sees positional mismatch -> reads ChatPanel.jsx
                ("is this the only caller?" — NOW cannot ask this)
       CALL 4   -> #1 api.js:49       high   0.85
                   #2 api.js:58       medium 0.70
                   #3 api.js:62       low    0.55
       Agent B "is this code robust"  (parallel)
       CALLS 5-7 -> #4 retriever.py:115 high   0.80
                    #5 retriever.py:119 medium 0.72
                    #6 retriever.py:121 medium 0.82              5.1s

[7b]   REPEATS, then SEVERITY GATE — both before any verification
       #3 low severity -> DROPPED. Never verified; the gate would
       have dropped it anyway, so verifying it is pure waste.
       5 candidates survive.

[8]    route: high severity OR confidence < 0.75 -> agentic
       #1 high · #2 0.70 · #4 high · #5 0.72   -> agentic
       #6 medium and 0.82                       -> cheap judge

       LOOP 1 api.js (#1,#2)       CALLS 8-10
              #1 SUPPORTED
              #2 CONTRADICTED — read line 71, onError IS reached
       LOOP 2 retriever.py (#4,#5) CALLS 11-13
              #4 SUPPORTED
              #5 UNVERIFIABLE -> 0.72 x 0.7 = 0.50
       CALL 14  cheap judge: #6 SUPPORTED                       15.7s

[9]    confidence gate (>= 0.6):
       #1 0.85 POST · #4 0.80 POST · #6 0.82 POST
       #5 0.50 HOLD                                              0s

[10]   CALLS 15-17  three suggestions, all validated and offered
[11]   CALL 18      summary                                      1.8s
[12]   one batched review: 3 inline comments + summary           1.0s
                                                         total  28.3s
```

**Call 10 is the point of the design.** The verifier killed #2 by reading
api.js:71, which is outside the pre-built context window.

What would NOW do with #2? Its judge sees the diff and the pre-built
context, neither of which contains line 71. On that evidence the finding
is plausible, so the likely verdict is **`supported`** at 0.70 — above
the 0.6 gate, and posted. A wrong comment ships.

Had the judge instead returned `unverifiable`, 0.70 x 0.7 = 0.49 would
have held it. So the failure mode is not the judge abstaining; it is the
judge being **confidently wrong on evidence it cannot reach**. That is
precisely what a tool loop fixes.

### Case 2 — Docs-only PR (0 calls, $0)

Skip rules end the review at stage 3. The cheapest review never reaches a
model, which is why the rules run before everything.

### Case 3 — Config-only PR (1 call, $0.0003)

Triage returns `run: []`. Without triage this would wake two agents and
cost $0.0089.

### Case 4 — Clean PR, no findings (8 calls, $0.0097)

Specialists return nothing, so verification, the fixer and the gate are
all skipped. 88% of config A's bill never happens.

### Case 5 — Re-push, all repeats (7 calls, $0.0089)

Same findings, `drop_repeats` removes all of them, nothing is posted.

**This case justified two design changes**, both of which move work
*before* verification:

1. **Repeats** — originally after the gate, costing 14 calls to discover
   there was nothing new to say. Now immediately after stage 7: 7 calls.
2. **Severity gate** — a low-severity candidate the gate will drop must
   never reach the verifier. Only the *confidence* check stays after,
   because verification is what sets final confidence.

The rule generalises: **never pay to verify something that cannot be
posted.**

### Case 6 — Worker crashes mid-review

```
CALLS 1-7 done, 50,400 tokens spent, then the worker dies.

NOW:  recovery.py re-runs from stage 3 -> all 7 calls repeated.
v2:   the checkpoint after "specialists" holds the 6 candidates.
      recovery.py resumes thread "12:42:a3f9c21" at the verifier.
      CALLS 8-17 only. 50,400 tokens saved, 49% of the run.
```

### Case 7 — Large PR, 40 files (24 calls, $0.2546 config A)

18 files skipped by rules, 22 reviewable. Both agents run to
`MAX_TURNS_PER_AGENT = 6` on a larger diff: **144,000 input tokens**.
Eight candidates across five files give five verifier loops: **91,500**.
Total 235,500 — **2.3x the 18-call case**, and the most expensive
scenario by a wide margin.

This is the scenario that proves a single per-review token cap is wrong:
at 150,000 the specialists alone would exhaust it and verification would
never run. Budget per stage.

If a stage budget does trip, publish what is verified and say in the
summary what was not reached. **Degrade, never guess** — unverified
findings never fill the space.

### Case 8 — Everything contradicted (15 calls, ~$0.10 config A)

The verifier contradicts every candidate; nothing is posted. This looks
like waste and is not: it is ten cents spent **not** posting four wrong
comments. Precision is the north-star metric and this is what buying it
looks like.

### What the scenarios establish

1. **Cost scales with what is found, not with PR size.** Skip rules and
   triage make a large boring PR cheap.
2. **The expensive stage is conditional.** No candidates, no
   verification.
3. **The gates are the engineering.** A naive design that wakes four
   agents on every PR costs 3x as much for the same architecture.


## What changes, what stays

| Component | Fate in NEW |
|---|---|
| `src/webhooks/` | **unchanged** |
| `src/diff/` | **unchanged** |
| `src/github/` | **unchanged** |
| `src/context/` | **re-exposed as tools**, logic unchanged |
| `src/filter/gate.py`, `repeat.py` | **unchanged** — stays deterministic |
| `src/filter/suggest.py` | unchanged, gains a model call upstream |
| `src/review/provider.py` | **kept**, called from inside nodes |
| `src/classification/` | becomes the triage node |
| `src/review/generate.py` | becomes two specialist agents |
| `src/judge/verify.py` | becomes the verifier agent, with tools |
| `src/pipeline.py` | becomes a LangGraph `StateGraph` |
| `src/recovery.py` | kept as the scanner; resumes a thread, not a run |
| *(new)* summary node | added |
| *(new)* toolbox layer | added |

Roughly 70% of the codebase is untouched. The change is concentrated in
orchestration and in the three stages that call a model.


## Why LangGraph — and why the case is now weak

LangGraph is a state machine whose state is checkpointed to Postgres
after every node. Nodes are plain Python functions, so adopting it does
not require adopting LangChain's model wrappers; `provider.py` runs
inside a node unchanged.

**The original justification does not survive v2.** It rested on two
claims, both built on v1's numbers:

| Claim, under v1 | Under v2 |
|---|---|
| Four agents writing one list needs a reducer | Two agents. `asyncio.gather` and a list concat |
| 70% of cost sits before verification, so resuming matters | ~9% sits there. Resuming the specialists saves **$0.0089** |

Both collapse. Two concurrent coroutines and one database write of the
candidate list after stage 7 give the same guarantees with no framework.

What survives is narrower and should be argued on its own merits:

- **Per-file verifier checkpoints**, where the real $0.0456 sits — but a
  row per completed loop does this too.
- **Trajectory tracing.** Agent debugging needs the whole turn sequence,
  and LangSmith provides it. `src/common/trace.py` would have to grow.

**Verdict: not justified on cost.** Treat LangGraph as a choice to
defend with measurement, not a given. The line that still holds is the
architectural one: **agents go where the work is open-ended** (what
should I look at?), **plain code goes where the work is known** (what
happens next?). The orchestrator is not an agent either way.

## Risks

| Risk | Mitigation |
|---|---|
| Calibrated confidence is lost | Keep `find_references` as a tool so its score reaches the model as evidence; require cited tool output |
| Agent loops are not unit-testable | Test the tools exhaustively; eval the loops statistically; keep both architectures behind flags |
| Silent failure — agent reads wrong files, returns nothing | Record the trajectory; alert when an agent returns zero findings after N turns |
| Free-tier rate limit | Reduced configuration, or Developer plan at ~$5/month |
| **Failure surface grows 17x** | Every call is a chance to fail. Retry per turn with the conversation intact; a failed turn must not restart the loop |
| **Debuggability** — a trajectory, not a request | Persist every turn's tool calls and results against the review id; `src/common/trace.py` prints the trajectory, not just the result |
| **RPM budget**, not just TPM | 18 calls against a requests-per-minute limit; `provider.py` must throttle on both headers |
| Non-determinism compounds across turns | F1 already swings 11 points at temperature 0; measure over repeated runs, never a single trace |
| Framework version churn | Pin LangGraph; keep business logic out of nodes |
| Two state stores diverge | Checkpoint state is for resuming only; `reviews` and `findings` stay the reporting source of truth |


## Measurement

Both architectures stay behind environment flags, as `CONTEXT_ENABLED`,
`JUDGE_ENABLED` and `SUGGESTIONS_ENABLED` already are. The evaluation
harness in `src/eval/` runs the same cases through both and reports
precision, recall, comments per PR, latency and cost.

A measured comparison of the two designs is worth more than either design
alone, because it answers *whether agents help here* with a number rather
than an opinion.
