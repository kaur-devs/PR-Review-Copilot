# Product Requirements Document (PRD)

## Product Goal
Create an installable AI reviewer that reads a pull request the way a careful senior engineer would, verifies its own findings, and posts them as real, severity-gated GitHub comments.

## Feature Priorities
| Feature | Priority |
|---|---|
| GitHub App installation | Must |
| Webhook-triggered diff ingestion | Must |
| Connected-file context retrieval | Must |
| Task-decomposed review generation | Must |
| Judge/grounding pass | Must |
| Confidence + dedup filtering | Must |
| Severity-gated comment posting | Must |
| Outcome capture | Must |
| Evaluation harness | Must |
| PR-level summary comment | Should |
| Feedback-driven suppression | Should |
| Deeper cross-file graph | Could |
| Autofix suggestions | Won't (v1) |

## User Stories

### US-001
As an engineering team lead, I want to install the reviewer on my repository so that every future PR gets reviewed automatically.

**Acceptance criteria**
- Installation completes through GitHub's App flow.
- The app requests only PR read/write and contents-read permissions.

### US-002
As a developer, I want the reviewer to understand code connected to my change, not just the diff, so that it catches cross-file breakage.

**Acceptance criteria**
- Findings can reference a file that wasn't itself changed in the PR.

### US-003
As a developer, I want the reviewer to double-check itself before commenting so that I'm not shown false alarms.

**Acceptance criteria**
- A finding that fails the judge pass never reaches the PR.

### US-004
As a developer, I want only important issues shown by default so the PR stays readable.

**Acceptance criteria**
- Low-severity findings are stored but not posted unless explicitly requested.

### US-005
As a builder, I want a measured evaluation report so I can state a credible recall/false-positive number.

### US-006
As a team, I want the bot to stop repeating a finding category we keep dismissing (v2).

## Product States
`INSTALLED → WEBHOOK_RECEIVED → PROCESSING → REVIEW_POSTED → OUTCOME_RECORDED`

Failure state: `PROCESSING_FAILED`.

## Product Principles
- Grounded before posted — every finding is verified against real code before a human sees it.
- Human-in-the-loop — the tool comments, it does not block or auto-merge.
- Transparent, not novel — built from documented competitor techniques, cited as such.
- Quiet by default — severity-gating over completeness.
- Measured, not claimed — every quality statement is backed by the eval harness.
