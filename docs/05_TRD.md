# Technical Requirements

## Stack

| | Choice | Notes |
|---|---|---|
| Backend | Python 3.14, FastAPI | async, webhook-shaped |
| GitHub | GitHub App — webhooks and REST, scoped permissions | ADR-001 |
| Model provider | Groq, OpenAI-compatible, behind our own client | ADR-003 |
| Generation | `openai/gpt-oss-120b` | classification, specialists, summary |
| Verification | `qwen/qwen3.8-27b` | a different family, deliberately |
| Orchestration | LangGraph, for flow between stages only | ADR-011 |
| Diff parsing | `unidiff` | |
| Code understanding | `tree-sitter` — Python, JavaScript, TypeScript, TSX | |
| Database | PostgreSQL (Neon) | |
| Migrations | Alembic | |
| Hosting | **not deployed** | runs on a laptop |

Swapping provider is four environment variables: `LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_MODEL`, `LLM_JUDGE_MODEL`.

## Why these

**FastAPI.** The service is a webhook endpoint that must answer inside ten
seconds while the real work continues behind it. Async and background tasks
are the whole requirement.

**A GitHub App, not a token.** An App has its own identity and
per-installation scoping, which is what a thing other people install needs.
A personal token is tied to an account and cannot be distributed.

**Free-tier models behind our own client.** There is no budget for paid
inference, so every stage runs on a free tier. The client is ours rather than
a library's because the binding constraint is tokens per minute: it has to
reserve budget *before* sending and distinguish a minute's limit from a
day's. Libraries that retry after a refusal make that worse.

**PostgreSQL, not a document store.** Reviews, findings and outcomes are
related by foreign keys, and the idempotency guarantees are unique
constraints. Both are things a relational database does and a document store
imitates.

**LangGraph for flow, our code inside it.** Two reviewers run concurrently
and a verifier loops, so the sequence branches and is worth resuming
part-way. LangGraph owns the edges between stages; every HTTP call to a model
stays ours. Full reasoning in ADR-011, which supersedes the earlier decision
to use no framework at all.

## Requirements

| ID | Requirement | Status |
|---|---|---|
| TR-001 | Webhook endpoint with signature verification | built |
| TR-002 | Diff fetch and structured parsing | built |
| TR-003 | Connected-file context from a repository snapshot | built |
| TR-004 | Change classifier, batched to fit the token window | built |
| TR-005 | Structured, validated output for every model reply | built |
| TR-006 | Verification by a second model, with abstention | built |
| TR-007 | Severity and confidence gates; repeats dropped | built |
| TR-008 | One batched review, comments by line and side | built |
| TR-009 | Outcome-tracking schema and thread events | built |
| TR-010 | Evaluation harness with ablations | built |
| TR-011 | Tool loop with bounded turns, calls and tokens | built |
| TR-012 | Per-model token budgets, reserved before sending | built |
| TR-013 | Resume a review interrupted by a restart | built |
| TR-014 | Retire a review when a newer commit arrives | built |
| TR-015 | Multi-language parsing: Python, JS, TS, TSX | built, not evaluated |
| TR-016 | Deployment GitHub can reach | **not built** |
| TR-017 | Per-repository suppression from outcomes | **not built** |
| TR-018 | Per-repository configuration file | **not built** |

## Non-functional targets

| | Target | Measured |
|---|---|---|
| Review latency | under 60s | **428s** on the free tier, nearly all of it waiting for token budget |
| Tokens per review | — | ~65,000 on a four-file pull request |
| Secrets | never in source | `.env`, gitignored; key file path, not the key |
| Source code in logs | only what a specific failure needs | holds |

The latency target is not met and will not be on a free tier: 8,000 tokens a
minute against a review that wants 65,000 is nine minutes of waiting before
any thinking. It is a tier limit, not a code limit — the same review on a
paid tier fits inside one minute's budget. Stated rather than quietly
dropped.

**Not met: provenance.** Nothing records which model or prompt version
produced a finding. The `findings` table has no column for it, so a result
cannot be traced back to what generated it. For a project whose argument is
"measured, not claimed", that is the most important of these gaps.
