# Feature Roadmap — Beyond the PRD

The PRD scopes a reviewer that comments. This document scopes the features
that would make it stand out, and says plainly which ones change what the
project is rather than merely adding to it.

Written after a mentor review raised three fair criticisms: the tool only
comments instead of fixing, it uses no framework, and it has no answer for
code that breaks after merge. Each is addressed below.

## The asset to exploit

Most LLM reviewers are a model behind a prompt. This one already holds three
things such a wrapper cannot have:

| Asset | Where it comes from |
|---|---|
| A tree-sitter AST of the repository at the review commit | `src/context/symbols.py` |
| The repository itself, extracted on disk | `src/context/snapshot.py` |
| An ablation harness that can measure whether a change helped | `src/eval/run.py` |

Every feature below is ranked by how much it leans on those three, because
those are the parts that cannot be copied by prompting a model harder.

The through-line: **an AI reviewer that proves its findings by running them,
and measures what each part of its architecture contributes.** A feature
either serves that sentence or it does not belong here.

---

## Tier 1 — changes what the project is

### 1. Execution-grounded verification

The judge currently asks one model whether another model's finding is
supported. That is a second opinion, which is why it once deleted a true
finding on correct reasoning from truncated evidence.

The repository is already on disk. So stop asking and run the code.

1. Ask the model for a minimal failing test that triggers the finding.
2. Run it against the snapshot in a subprocess.
3. Decide by what happened.

| Outcome | Verdict | Action |
|---|---|---|
| Test fails with the predicted error | `PROVEN` | post it, with the test attached |
| Test passes | `DISPROVEN` | drop the finding silently |
| Test errors for an unrelated reason | `UNVERIFIABLE` | fall back to the LLM judge |

Why it matters: a reproducing test is categorically different evidence from a
model's assertion. It also yields a precision figure that needed no hand
labelling, because every `PROVEN` finding is a true positive by construction.

Honest limit: only executable findings qualify — crashes, wrong return
values, boundary conditions. A breaking change across files usually cannot be
tested this way. The reportable number is therefore "N% of findings were
execution-proven, the rest fell back to the judge", which is a stronger claim
than a single unexplained score.

Risks to handle: untrusted code needs a subprocess with a hard timeout, no
network, and a temporary directory. Dependencies may not install. Every
failure degrades to `UNVERIFIABLE` rather than to a crash.

### 2. Conversational re-judging

Today a developer who replies "this is handled upstream in middleware.py" is
answered with silence.

Instead, on a reply to one of our comments:

1. Read the reply as new evidence.
2. Fetch any file it names, from the snapshot we already hold.
3. Re-run the judge with the reply and that file in context.
4. Concede — resolve the thread and say which line changed our mind — or hold,
   quoting the line that contradicts it.

Conceding is the feature. A bot that withdraws a finding with a reason is
trusted in a way one that argues is not. It also produces labelled data for
free: every concession is a false positive with a human-written explanation.

Extends the `pull_request_review_thread` handling already built to
`pull_request_review_comment`.

### 3. A real benchmark and a head-to-head

Four cases, one repository, bugs written by the author, against a measured
eleven-point F1 noise band. This is the weakest claim in the project.

**Public dataset.** Mine real pull requests where a merge was followed by a
bug-fix commit touching the same lines; the fix commit is the ground truth.
Filter a few hundred candidates down to roughly thirty clean Python cases.
Blocked on personal-access-token support in the harness, since an
installation token cannot reach a stranger's repository.

**Naive baseline.** Add `H4`: the diff, a one-line prompt, no context, no
classification, no judge. The results table then reads as a contribution
breakdown rather than a single score.

| Configuration | Precision | Recall | F1 |
|---|---|---|---|
| naive prompt | | | |
| + connected context | | | |
| + classification | | | |
| + judge | | | |
| + execution proof | | | |

Report it with the variance band. The eleven-point swing on identical input
is itself a finding, and most projects never discover theirs.

---

## Tier 2 — the deterministic layer

No model involved. Computed from the AST and from git, so these are fast,
free, always right, and structurally impossible for a prompt-only reviewer
to produce.

| Feature | What it reports | Built from |
|---|---|---|
| Blast radius | `Chunker.chunk` is called from 14 places across 6 files, 2 in `api/` | transitive references, `src/context/references.py` |
| Hotspot history | this function has 4 bug-fix commits in 3 months; line 113 was last changed in a commit that was reverted | `git log`, `git blame` on the snapshot |
| Coverage gaps | 8 of your 23 changed lines have no test, including the new `except` branch | `pytest --cov` in the snapshot, or a CI `coverage.xml` |
| Criticality weighting | severity scaled by how many files import the changed module | one tree-sitter pass over the snapshot |
| Cross-PR conflict | PR #47 also modifies this function; no textual conflict, possible semantic one | `GET /pulls` plus a symbol intersection |

Coverage gaps are worth singling out: it is the only finding type here with a
zero false-positive rate, which is worth stating because every other finding
the tool emits has one.

---

## Tier 3 — makes it a copilot

### Suggested changes, gated harder than comments

A GitHub suggestion is a fenced block inside a review comment, and the
developer applies it in one click. That is the current baseline for this
product category, not an advanced feature.

The implementation is small: one `suggestion` field on `Finding`, one line in
the prompt schema, four lines in `build_comment_body`.

The gate is the interesting part. A wrong comment costs ten seconds of
reading; a wrong suggestion costs one click. So a suggestion requires high
severity, confidence at or above 0.8, and a judge verdict of `supported`
rather than `unverifiable` — and the replacement must parse under tree-sitter
before it is posted, since an unparseable suggestion produces broken code on
apply.

### Repository conventions

Sample existing files from the snapshot and derive this repository's own
conventions — error handling, logging, type hints, docstring style — then
review against those rather than against generic ones. Cached per repository.

Addresses the standing complaint about AI reviewers: generic advice. "This
codebase raises typed exceptions everywhere else; this function returns None
on failure" is a reviewer that read the code.

### Intent versus implementation

Read the title, body and linked issue; check the diff against the claim. One
extra call on text the webhook already delivers. Catches scope creep,
forgotten files, and descriptions copied from another pull request.

### Questions instead of claims

The judge's `unverifiable` findings are currently posted as assertions at
reduced confidence. Post them as questions instead. A wrong question costs
nothing; a wrong assertion costs trust. Two lines of prompt, using a
distinction already built.

### Suppression, closed

`repo_suppressions` is a table nothing reads or writes. Count dismissals per
repository and category, flip the flag past a threshold, check it in the gate,
and announce it in the review body. A bot that notices it is being ignored and
backs off is memorable.

### Commands and configuration

`@bot review again`, `@bot explain`, `@bot ignore <category>`, and a
`.prreview.yml` for thresholds and path excludes. Table stakes for the
category; their absence is noticed.

---

## Tier 4 — research-shaped

### Confidence calibration

The tool emits confidence scores. Whether they mean anything is testable:
bucket findings by stated confidence and compare against the acceptance rate
recorded in `outcomes`.

| Stated | Accepted |
|---|---|
| 0.9–1.0 | |
| 0.7–0.8 | |
| 0.6–0.7 | |

A roughly diagonal result means the scores are calibrated. A flat one means
they are decorative — a genuinely interesting negative result, and finding it
out is better than assuming otherwise. Costs one query against existing
tables.

### Regression memory

"Line 113 was flagged on PR #42 and dismissed. The bug fixed in this pull
request is on line 113." Needs nothing new: `findings`, `outcomes`, and a
line match.

### Post-merge ground truth

The sound form of the production question. When a pull request this tool
reviewed is merged and then reverted or linked to an incident, record it as a
measured false negative. Real recall data from production rather than from
cases written by the author.

Note what this is not: pushing a fix to a broken production branch. The safe
automated response to a production break is a revert, which is deterministic,
not a generated fix, which is a guess applied unreviewed while the system is
already down. The defensible version of this feature opens a revert pull
request and lets a human merge it.

### Review the review

A final pass before posting: would a senior engineer find these comments
useful or annoying? Prune accordingly. The project's stated posture is quiet
by default; this enforces it at the last gate rather than only the first.

### Multi-model consensus

Three free-tier models from different families, posting only where two agree.
Costly in tokens, free in money, and it yields an inter-model agreement rate —
another number this product category does not report.

---

## Sequence

| Phase | Features | Why this order |
|---|---|---|
| 1 | execution grounding, public benchmark, naive baseline, calibration | changes the project's category |
| 2 | blast radius, hotspot history, coverage gaps, criticality weighting | highest value per hour; no model, so nothing fails nondeterministically |
| 3 | suggested changes, conversational re-judging, questions, suppression | what the word copilot actually means |
| 4 | configuration, commands, dashboard, deployment | what makes it demonstrable |

## On frameworks

LangChain was considered and not used. The reasons, recorded so the question
has an answer rather than an absence:

- The binding constraint here is tokens per minute, not tokens per request.
  LangChain's rate limiting is request-based and has no first-class way to
  read `x-ratelimit-remaining-tokens` from a response and pace the next call
  against the remaining window. That logic would have been written anyway,
  then fought into the framework.
- This is not a retrieval-augmented project. Retrieval here is a two-pass
  tree-sitter search with a confidence table, not embedding similarity.
  LangChain's retrievers are built for vector stores and contribute nothing
  to the part of this project that is actually difficult.
- Three model call sites against a 312-line adapter did not justify the
  dependency. With several call sites, or a team, it would have.
- Reading the exact bytes sent to the model mattered. The judge once deleted a
  true finding because its evidence had been truncated, and that was found by
  reading the prompt.

What was given up: tracing, and structured-output retries. Smaller versions of
both were written — `src/common/trace.py` and
`src/review/provider.py`. `LLMProvider` is the only module that talks to a
model, so this is not a one-way door.

## Scope discipline

This is a menu, not a backlog. Execution grounding, the deterministic layer,
and a real benchmark would together be a stronger project than adding every
remaining item at lower depth.
