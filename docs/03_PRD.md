# Product Requirements

## Goal

An installable GitHub App that reads a pull request the way a careful senior
engineer would, checks its own findings against the code, and posts the ones
worth a developer's attention as inline comments.

## Principles

- **Grounded before posted.** Every finding is checked against real code by a
  second model before anyone sees it.
- **Human in the loop.** It comments. It never blocks a merge or merges
  anything.
- **Quiet by default.** Severity-gated. A reviewer that posts everything is
  one nobody reads.
- **Measured, not claimed.** Quality statements come from the evaluation
  harness, and name the model that produced them.
- **Honest about what it did not do.** A review that could not read the
  repository says so rather than quietly reviewing less.

## Features

| Feature | Priority | Status |
|---|---|---|
| GitHub App installation | Must | built |
| Webhook-triggered diff ingestion | Must | built |
| Connected-file context | Must | built |
| Targeted review generation | Must | built |
| Verification before posting | Must | built |
| Severity and confidence gates | Must | built |
| Repeat suppression across commits | Must | built |
| Severity-gated inline comments | Must | built |
| Pull-request summary comment | Must | built |
| Outcome capture | Must | built |
| Evaluation harness | Must | built |
| Suggested replacement lines | Should | built |
| Multi-language review | Should | built, not evaluated |
| Linter pass before the model | Should | built |
| Feedback-driven suppression | Should | built |
| Per-repository configuration | Should | built |
| Deployment | Must | **not built** |
| Deeper cross-file graph | Could | not built |
| Autonomous fixing or merging | Won't | out of scope by principle |

Suggested replacement lines were originally out of scope. They are in because
a one-line fix behind GitHub's Apply button is cheap to offer and easy to
refuse: the suggestion is applied to the real file and must compile, and a
refused suggestion never hides the comment carrying it.

## User stories

### US-001 — Install it
As a team lead, I install the reviewer on a repository so every future pull
request is reviewed automatically.

- Installation completes through GitHub's App flow.
- It requests pull requests read/write and contents read, nothing else.

**Built.**

### US-002 — See past the diff
As a developer, I want findings about code my change breaks, not only lines I
touched.

- A finding may reference a file the pull request did not change.
- The comment sits on the changed line that caused the break and names the
  other end.

**Built.** The reviewer reads the repository at the commit under review and
can grep, open files and follow references while deciding.

### US-003 — Not be shown false alarms
As a developer, I want the reviewer to check itself before commenting.

- A finding the verifier contradicts never reaches the pull request.
- A finding it cannot check is kept at reduced confidence, not deleted and
  not trusted.

**Built.**

### US-004 — Only important issues
As a developer, I want the pull request to stay readable.

- Low severity and low confidence are stored but not posted.
- The summary says how many were held back, so nothing is hidden silently.

**Built.** A repository sets its own thresholds in `.pr-review.yml`:
which severities reach the pull request, the confidence floor, categories
to leave alone, and how many comments at most. Adding `low` to
`severities` is how a team asks to see everything. The file lives in the
repository under review, so the people being commented on own the
settings and a branch can change them.

### US-005 — A credible number
As a builder, I want a measured recall and false-positive rate I can defend.

**Partly built.** The harness, the scoring and the ablations exist. Four
labelled cases is too few to separate a real improvement from noise, and all
four are Python.

### US-006 — Stop repeating what we dismiss
As a team, I want the reviewer to fall silent on a category we keep
dismissing.

**Built, and deliberately slow to act.** A category is silenced for a
repository once at least ten findings in it have been posted and fewer
than one in five were acted on.

The evidence needs care. GitHub never says "this comment was wrong" — only
that a thread was resolved — so a rejection cannot be stored, and an
acceptance rate over stored rows alone would always read 100%. The
denominator is therefore every finding posted in that category and the
numerator is the ones somebody resolved. A suppressed finding is held, not
deleted, so lowering the threshold recovers it without re-reviewing.

It will still say nothing until a repository has real history, which is
correct rather than a limitation.

## What a review produces

One GitHub review containing:

- an opening comment: what changed, what is worth a look, how many comments
  follow and how many were held back
- one inline comment per finding, at a changed line, carrying severity,
  category, the problem, the reasoning and the files that were read
- a suggested replacement line where one is safe

## States

```
installed ──> delivery received ──> claimed ──> reviewing ──> posted
                     │                 │            │
                     ├──> ignored      └──> superseded
                     └──> duplicate                 ├──> skipped
                                                    ├──> nothing to say
                                                    ├──> all said before
                                                    └──> failed
```

`all said before` is deliberately distinct from `nothing to say`: a review
that found the same three problems again is not a review that found nothing.

## Out of scope

- Merging, blocking a merge, or approving.
- Executing any code from a pull request.
- Reviewing anything but the diff and the repository at that commit.
- Replacing a human reviewer. It is a second pair of eyes that never gets
  tired, not a senior engineer.
