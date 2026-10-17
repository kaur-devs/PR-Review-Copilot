# Complete Flow — Viva Script

Every step that is built, in the order it runs. For each: what happens, what
to say, and a concrete detail worth having ready.

Only completed work is included. Deployment is not built.

---

## Opening — the project in three sentences

> It is an AI code reviewer that installs as a GitHub App. When you open a
> pull request it reads the change, works out which other files depend on
> what changed, and posts inline review comments.
>
> The bet is that most AI reviewers only read the diff, and a diff is not
> enough: if a function changes from raising an error to returning nothing,
> the bug is in every caller that assumed a real value — and those callers
> are not in the diff.
>
> Everything runs on free-tier hosting and free-tier models, so the scarce
> resource is requests and tokens per minute, not money.

**Numbers to have ready:** 321 tests, 4,850 lines of source, 7 database
tables, 15 merged pull requests, 3 model calls per review.

---

## Step 0 — Setup: the App and the installation

**What happens.** A GitHub App is registered once. Accounts install it, and
each installation grants access to chosen repositories.

**Say this:**
- A GitHub App is one registration that many accounts install.
- Installing is a permission grant: read code, comment on pull requests.
- It installs on an **account** — personal or organisation — and repositories
  are chosen inside that.
- Permissions are scoped to three: pull requests read and write, contents
  read, metadata read. Nothing else was requested.

**Detail.** Installation `166408152`, on `kaur-devs`, selected repositories.
That number is the tenant key — it is how the service knows whose code it is
looking at.

---

## Step 1 — Proving identity to GitHub

**What happens.** The private key is turned into permission, in two steps.

**Say this:**
- Step one: sign a short note with our private key saying "I am this App".
  Valid nine minutes. It proves identity and **opens nothing**.
- Step two: trade that note for an installation token. Valid one hour, scoped
  to one installation, bounded by the permissions granted at install time.
- Two steps rather than one because the second token is narrow and
  short-lived. If it leaks it expires within the hour and only ever touched
  one account. The private key never leaves the server.

**Details worth having:**
- The note is a JWT signed RS256. Three claims: `iat` backdated 60 seconds
  for clock drift, `exp` at nine minutes because GitHub refuses more than ten,
  `iss` set to the Client ID.
- The header word differs: `Bearer` for the note, `token` for the installation
  token. Getting that wrong gives a confusing 401.
- Tokens are cached per installation and replaced at 55 minutes, so a request
  never starts with one about to expire. First fetch ~3 seconds, cached 0ms.

---

## Step 2 — A webhook arrives

**What happens.** GitHub POSTs to one address when a pull request changes.

**Say this:**
- GitHub pushes to us. We do not poll.
- It allows **ten seconds** to reply and **never retries** a failed delivery.
  Those two facts shape the whole architecture.
- The payload says something happened. It contains **no code** — we fetch that
  ourselves.

**Details:**
- Three headers matter: `X-GitHub-Event`, `X-GitHub-Delivery` (a unique id),
  `X-Hub-Signature-256`.
- The payload is thousands of lines; we read nine fields: action, number,
  head sha, base sha, repository id, repository full name, installation id.
- Four actions mean new code: `opened`, `synchronize`, `reopened`,
  `ready_for_review`. Labelling or assigning changes nothing.

---

## Step 3 — Proving the message is genuine

**What happens.** HMAC-SHA256 over the raw bytes, compared in constant time.

**Say this:**
- The webhook address is public. Anyone who finds it can POST to it.
- GitHub and we share a secret. GitHub mixes it with the exact bytes of the
  message to produce a fingerprint. We do the same sum and compare.
- Nothing that fails this check ever reaches the database. It is logged and
  discarded.

**The two details that matter:**
- We hash the **raw bytes**, not parsed JSON. Re-encoding a parsed payload
  changes whitespace and key order, and the fingerprint stops matching. There
  is a test that proves exactly this, because it is the easiest way to break
  the service while the code still looks correct.
- Compared with `compare_digest`, not `==`, so the time taken does not leak
  how many leading bytes were right.

---

## Step 4 — Not doing the same work twice

**What happens.** Two independent duplicate checks, both enforced by the
database.

**Say this:**
- There are two different ways the same thing arrives twice, so there are two
  protections.
- The **delivery id** catches the same message twice — someone pressing
  Redeliver.
- **(repository, pull request, commit)** catches two different messages about
  the same code — a pull request reopened with nothing pushed.
- Both return **200, not an error**. GitHub reads any non-2XX as a failed
  delivery, so a 409 on correct behaviour would mark the service as broken.

**The point worth making:**
- Uniqueness is enforced by the database, not by code. Checking first and then
  writing looks correct and is not: two messages arriving at the same instant
  both see nothing and both insert. Every write says "insert this, and if it
  clashes, do nothing", so exactly one wins.

**Detail.** In one test run five messages produced four rows and three
reviews. Review ids skipped from 1 to 3 — a rejected insert still consumes a
sequence number, which is normal Postgres behaviour, not a lost row.

---

## Step 5 — Reply first, work second

**What happens.** The 200 goes back, then a background task starts.

**Say this:**
- The reply is sent before any real work. Fetching files, downloading the
  repository, calling the model — all after.
- If it were the other way round, every review would take 30 seconds, blow
  the ten-second budget, and GitHub would record a failure it never retries.
- The background task receives a pull request and a review id, **never an
  HTTP request**. That is what lets the evaluation harness replay historical
  pull requests with no webhook involved.

**Detail.** Moving the review out of `received` into `processing` *is* the
lock. If that move fails, something else owns the work and this run stops
having made zero API calls.

---

## Step 6 — Fetching what changed

**What happens.** `GET /repos/{owner}/{repo}/pulls/{n}/files`.

**Say this:**
- This is an API call we make, not a webhook. Opposite direction.
- It returns a JSON array, one object per changed file.

**The response, concretely:**
```json
[{"filename": "backend/app/services/chunker.py",
  "status": "modified",
  "additions": 4, "deletions": 4, "changes": 8,
  "patch": "@@ -33,7 +33,7 @@\n-  \"code_text\": code,\n+  \"content\": code,",
  "sha": "e189461...", "blob_url": "...", "raw_url": "...", "contents_url": "..."}]
```

**Three things to handle:**
- It arrives in **pages**, 100 at a time, followed via the `next` link.
- GitHub caps the response at **3,000 files with no error**. Reaching exactly
  that count marks the set truncated.
- `patch` is **optional**. GitHub omits it for binary files and for diffs that
  are too large. On one real pull request `package-lock.json` had 4,391
  changes and no patch at all.

---

## Step 7 — Throwing away what nobody wants reviewed

**Say this:**
- Six rules: deleted, no diff, lockfile, vendored directory, machine-generated,
  over 500 changed lines.
- Every file kept costs a model request from a daily cap, so this is quota
  management, not tidiness.
- Each skip keeps its **reason**, so the logs say why rather than a file
  silently vanishing.

**Known gap, say it plainly:** the rules miss `.jsx`, `.svg`, `.css`,
`.gitignore` and similar. On a 65-file pull request that let about 40
unreviewable files through, and they are what blew the token limit at
classification.

---

## Step 8 — Reading the diff

**What happens.** Patch text becomes exact line numbers.

**Say this:**
- A `@@ -33,7 +33,7 @@` header says: 7 lines starting at 33 in the old file,
  7 lines starting at 33 in the new one.
- Two counters walk down together. A space line advances both, `-` advances
  only the old, `+` advances only the new.

**The detail that matters most:**
- **Every line carries two line numbers** — where it sits in the old file and
  the new one. A removed line has no new number.
- That is because of how comments are posted. GitHub deprecated the old
  positional system; comments now need a line number plus a side. Added lines
  are `RIGHT` with the new number, removed lines are `LEFT` with the old one.
- **GitHub only accepts a comment on a line that appears in the diff.** The
  parser records which lines are even eligible, and that becomes a validation
  check five steps later.

**Also:** GitHub's `patch` field omits the `---` and `+++` headers that the
parsing library requires, so we add them back. For a rename, the old name goes
on the `---` line.

---

## Step 9 — Finding the code that breaks

This is what the project is about. Four parts.

### 9a — Snapshot

**Say this:** we download the repository at the exact commit being reviewed.

**Three things GitHub does:**
- It is a **302 to a different host**, `codeload.github.com`.
- There is **no `content-length`**, so the 50 MB cap is enforced while
  streaming rather than checked up front.
- Everything sits under a prefix like `owner-repo-sha/`, which must be
  stripped.

Only `.py` files outside vendored directories are kept — 46 archive entries
became 23 Python files on one repository. It streams to disk, not memory,
because 50 MB in RAM on a 512 MB instance is asking for trouble.

### 9b — Which function changed

**Say this:**
- Line numbers cannot be searched for; function names can. So we convert one
  into the other.
- Parse the file with tree-sitter, find the smallest node at the changed line,
  walk up through parents until hitting a function or class definition.
- It also builds a qualified name from the whole parent chain, so a method
  knows its class: `User.get_email`, not an ambiguous `get_email`.

**The trap:** tree-sitter **never raises**. Feed it broken Python and it
returns a normal-looking tree full of ERROR nodes. The code asks explicitly
whether parsing succeeded, otherwise it would extract confident nonsense.

### 9c — Who uses it, in two passes

**Say this:**
- Pass one is a plain **text scan** over every file. No parsing. Over-returns
  on purpose and costs almost nothing.
- Pass two parses **only the survivors** and confirms what each match is.
- Two passes because parsing every file for every symbol is unaffordable on a
  tenth of a CPU. That split is forced by the hosting, not chosen for elegance.

**What it can tell apart:**

| Code | Verdict | Confidence |
|---|---|---|
| imports it, then calls it | real | 0.9 |
| imports it **renamed**, calls the new name | real | 0.9 |
| `models.get_user()` after `import models` | real | 0.9 |
| assigns it without calling | reference | 0.7 |
| calls it with no import anywhere | probably | 0.5 |
| `client.get_user()` — different object | rejected | 0.25 |
| the name in a comment or string | rejected | 0.1 |

**The case that justifies the library:** a renamed import. If the call reads
`fetch_user(...)`, the name searched for appears only on the import line. No
text search connects them — you have to read the import's structure.

### 9d — The bundle

Rank by confidence, cut to a 12,000-character budget, attach how each was
found. The snippet is the **whole enclosing function**, because what matters
is what the caller does with the result.

**Say this, it is a good story:** when a function is longer than the 60-line
cap, the window centres on the reference rather than starting at the top. That
came from a real failure — a caller was 86 lines, trimming from the top cut
the evidence off by one line, and a true finding was lost.

### Degradation

Three states stored on every review: `ok`, `degraded`, `unavailable`. Nothing
here can fail a review — `unavailable` is simply a diff-only review.

### The measured limitation — say this before being asked

> It finds who **calls** changed code. It does not find who **consumes
> changed data shapes**. On one pull request a renamed dictionary key broke
> three files and none were retrieved, because the consumers reference a
> string literal rather than a function name.

---

## Step 10 — Labelling the change

**Say this:**
- A migration and a UI tweak deserve different questions. Asking one generic
  question produces generic, ignorable comments.
- Seven labels: api, schema, dependency, config, logic, test, docs.
- **Path rules first**, costing nothing: `tests/` is a test,
  `alembic/versions/` is schema, `requirements.txt` is a dependency.
- Whatever is left goes to the model in **one batched request**, not one per
  file. Ten files is one request.

**Everything the model returns is checked:** an invented kind is rejected, a
forgotten file still gets a label, a file nobody asked about is ignored — that
last one also guards against prompt injection.

**Known gap:** batching *all* undecided files into one request does not scale.
On 56 files it was 14,874 tokens against an 8,000 ceiling and returned 413. It
degraded correctly to `logic`, but a fallback is not a result.

---

## Step 11 — Generating the findings

**Say this:** the model is given three things — the diff with real line
numbers, the connected code from step 9 labelled with how confidently it was
found, and questions chosen by the label.

One request per change kind, not per file. Generation is deliberately less
batched than classification, because focused context is where quality comes
from.

**The validation is where the real work is.** Six checks on every finding:

| Check | Catches |
|---|---|
| the file must have changed | invented filenames |
| **the line must be one GitHub accepts** | the important one |
| severity from the list | invented `"catastrophic"` |
| category from the list | invented `"vibes"` |
| message not empty | |
| confidence is a number, clamped 0–1 | `"very sure"` |

Each finding is checked independently, so partial garbage does not cost the
good ones. Every rejection is logged with its reason.

**Measured:** on one pull request it produced three findings, all three valid,
and said nothing about correct code that had been planted as a trap. Zero
false positives on that trap.

---

## Step 12 — The judge

**Say this:**
- Models produce confident, plausible, wrong findings routinely. Posting those
  is how a reviewer gets muted.
- A **different model** checks each finding against the code — `qwen` checking
  `gpt-oss`. A model asked to check its own work mostly agrees with itself.
- All findings in one request. Findings are numbered and verdicts come back by
  index, so matching cannot go wrong on text similarity.

**The story worth telling:**

> Originally the verdict was binary. On a real pull request it **deleted a
> true finding**, reasoning that no code in the provided context accessed the
> renamed key. That was true of the evidence it had — the proof had been
> trimmed out of the snippet by one line. Correct reasoning, incomplete
> evidence, a true finding silently destroyed.
>
> So the verdict is now three-way. `contradicted` deletes, and is only valid
> when the code shown positively disproves the claim. `unverifiable` keeps the
> finding with confidence multiplied by 0.7. The prompt says explicitly: not
> seeing the callers is not evidence that there are none.

**Graduated, not binary:** 0.97 becomes 0.68 and still posts; 0.80 becomes
0.56 and is stored but not shown.

**Fails open.** An error or unparseable answer keeps every finding. Silently
discarding real findings is worse than letting a weak one through, because the
first failure is invisible.

**Switchable** with `JUDGE_ENABLED=0`, which is hypothesis H2 in the harness.

---

## Step 13 — Deciding what to say

**Say this:**
- Every finding is stored **before** filtering, including ones that will not
  be posted, with a `posted` flag.
- That means thresholds can be tuned with a SQL query rather than re-running
  the pipeline. On a rate-limited free tier that decides how many tuning
  iterations fit in the schedule.
- The gate has two rules: severity must be high or medium (from the PRD), and
  confidence at least 0.6 (added on top, because a high-severity finding at
  0.3 is a guess).

---

## Step 14 — Posting

**Say this:**
- **One batched review**, so the developer gets a single notification rather
  than fifteen.
- `event: COMMENT`, **never** `REQUEST_CHANGES`. The tool comments, it never
  blocks a merge. There is a test asserting that, because it is a decision
  someone could casually undo.
- `line` and `side`, not the deprecated `position` — which is why every diff
  line carries two numbers.

**The detail nobody expects:** GitHub's create-review response **does not
include the individual comment ids**. Those ids are what later tells you
whether a developer resolved or dismissed a comment, so there is a follow-up
call to `/pulls/{n}/comments` filtered by review id.

**A bug only a real post could find:** the first live review matched 0 of 2
comments. Two causes — the `/reviews/{id}/comments` endpoint answers in the
legacy format with `line: null`, and two findings on the same line collapsed
under a keyed lookup. Both fixed, both tested.

---

## Step 15 — Outcome tracking

**Say this:**
- When a developer resolves one of our comment threads, GitHub sends a
  `pull_request_review_thread` event. We match its comment ids against the
  ones stored when posting.
- Resolving records the finding as accepted; reopening removes the record, so
  "accepted" always means "resolved right now".
- This is how the project measures whether it is useful rather than just
  producing output.

**Be honest:** built and tested against fakes, but **never fired on a real
thread**. GitHub does not document the thread payload's fields, so the code
builds to the expected shape and logs loudly on a mismatch.

---

## Step 16 — The evaluation harness

**Say this:**
- It replays pull requests where the answer is already known and scores what
  comes back.
- It was only possible because the pipeline takes a pull request, not an HTTP
  request. That one decision is what makes the project measurable.
- It writes nothing to the database and posts no comments.

**What it measures:**

| Metric | Formula |
|---|---|
| Precision | TP / (TP + FP) — of what it reported, how much was real |
| Recall | TP / (TP + FN) — of real bugs, how many it found |
| F1 | 2PR / (P + R) |
| Context recall | did it retrieve the file that mattered — **zero model calls** |

**Four configurations:** everything on, then context, judge and templates
switched off one at a time.

**The honest headline:**

> Three consecutive runs on identical input gave F1 of 74%, 71% and 82%. The
> model is not deterministic even at temperature zero, so at four cases
> anything moving F1 by less than about 15 points is noise. Context recall was
> exactly 50% every run, because it involves no model call — which makes it
> the metric worth tuning against.

That variance is the strongest argument for a 50-pull-request benchmark.

---

## Closing — what is not built

- **Deployment.** The only unbuilt feature.
- **No recovery for interrupted reviews.** The index for the scan exists; the
  scan does not.
- **Missed deliveries are invisible.** GitHub does not retry.
- **No deduplication across re-reviews.** Push twice, comments post twice.
- **Classification does not chunk**, so a large pull request falls back.
- **Skip rules miss non-Python files**, wasting tokens.
- **Thresholds are hand-chosen**, not calibrated.
- **Python only**, in three files of the context package.

Saying these before being asked is stronger than being caught by them. Each
one is in the HLD with a measurement or a specific pull request behind it.
