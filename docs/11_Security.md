# Security Design

## Authentication
GitHub App authentication via JWT (app-level) and installation access tokens (per-repo, short-lived).

## Authorization
GitHub App permissions, scoped to minimum necessary:
- `pull_requests: read & write`
- `contents: read`
- No other scopes requested.

## Data Protection
- No PR source code stored beyond what's needed for the specific review (findings/rationale, not full file contents).
- Secrets (GitHub App private key, LLM provider API keys) kept in environment/secret management, never committed.
- HTTPS for all external calls.

## Webhook Security
- Verify `X-Hub-Signature-256` on every incoming webhook.
- Reject unsigned or mismatched requests before any processing.

## API Security
- Signature verification middleware.
- Input validation on all internal endpoints.
- Rate limiting to protect against webhook replay abuse.

## AI Security
- Prompt injection defenses for untrusted diff/code content.
- Structured output schema validation.
- No arbitrary tool execution from PR content.
- Record model and prompt-template versions per finding for auditability.

## Important Product Rule
The system is a **review-support tool, not an autonomous merge gate**. It comments; it does not block, approve, or execute changes. A human developer remains responsible for the merge decision.
