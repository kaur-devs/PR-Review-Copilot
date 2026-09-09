# Technical Requirements Document (TRD)

## Proposed Architecture
- **Backend:** Python + FastAPI
- **GitHub integration:** GitHub App (webhooks + REST API), scoped permissions
- **LLM:** Free-tier hosted providers (Gemini / GitHub Models / Groq / Cerebras / Mistral), reached through a provider-agnostic adapter
- **Diff parsing:** `unidiff`
- **Database:** PostgreSQL (Supabase free tier)
- **Hosting:** Render (free tier)

## Technology Rationale

### FastAPI
Async-friendly, well-suited to a webhook-driven service, and matches existing Python fluency.

### GitHub App (not OAuth App or PAT)
Scoped, installable, per-installation permissions — the correct primitive for a product other repos install, unlike a personal-account-tied PAT.

### Free-tier LLM providers behind an adapter
Budget-driven architectural decision: the project has no paid-API budget, so all inference runs on free-tier hosted providers. Because free tiers differ in rate limits, structured-output support, and context window, the provider is reached exclusively through an adapter that abstracts authentication, structured JSON generation, token-usage reporting, and rate-limit/retry semantics. Different pipeline stages may run on different providers to spread request quota across accounts.

### PostgreSQL over a document store
The findings/outcomes/suppression data is relational by nature (foreign keys between reviews, findings, and outcomes) — a structured schema fits better than a flexible document model here.

### No agent framework (raw orchestration)
Direct API calls per pipeline stage, not a heavy agent framework — keeps the judge/grounding pass fully inspectable and avoids unnecessary abstraction over a pipeline that's fundamentally a fixed sequence, not an open-ended agent loop.

## Technical Requirements
| ID | Requirement | Maps to |
|---|---|---|
| TR-001 | Webhook endpoint with signature verification | BR-002 |
| TR-002 | Diff fetch + structured parsing | BR-003 |
| TR-003 | Connected-file context search | BR-004 |
| TR-004 | Change classifier + prompt template selection | BR-005 |
| TR-005 | Structured LLM output for findings | BR-005 |
| TR-006 | Judge/grounding pass | BR-006 |
| TR-007 | Confidence + dedup filter | BR-007 |
| TR-008 | Batched GitHub review comment posting | BR-008 |
| TR-009 | Outcome-tracking schema | BR-009 |
| TR-010 | Evaluation harness (recall/FP rate) | BR-010 |

## NFR Targets
- Typical PR (≤8 files) reviewed within ~60 seconds, excluding hosting cold-start.
- All model versions and prompt-template versions recorded per finding.
- Secrets (GitHub App private key, LLM API key) stored outside source code.
- No PR source code logged beyond what's necessary for debugging a specific failure.
