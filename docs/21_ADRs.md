# Architecture Decision Records

## ADR-001 — GitHub App, not OAuth App or Personal Access Token
**Context:** The reviewer needs to act on repos it doesn't own, with scoped permissions.
**Decision:** Register a GitHub App with minimal permissions (PR read/write, contents read).
**Rationale:** A GitHub App has its own identity and per-installation permission scoping; a PAT ties to a personal account and can't be distributed the same way.
**Trade-off:** More setup complexity than a quick PAT-based script.

## ADR-002 — FastAPI Backend, No Agent Framework
**Decision:** Orchestrate the pipeline with direct API calls in FastAPI, not a heavy agent framework.
**Rationale:** The pipeline is a fixed sequence (fetch → classify → generate → judge → filter → post), not an open-ended agent loop — a framework would add abstraction without solving a real problem here.
**Trade-off:** Some boilerplate that a framework might otherwise handle.

## ADR-003 — Free-Tier LLM Providers Behind a Provider Adapter
**Context:** The project has no budget for paid LLM APIs, and no local GPU, so inference must run on free-tier hosted providers.
**Decision:** Run every pipeline stage on free-tier hosted models (Gemini / GitHub Models / Groq / Cerebras / Mistral), accessed only through a provider-agnostic adapter. Paid model tiers are explicitly out of scope.
**Rationale:** Free tiers are capable enough for this pipeline, and the adapter makes the provider a configuration choice rather than an architectural commitment — so a provider can be swapped when rate limits or output quality demand it. Provider selection is empirical: a candidate is rejected if it cannot return schema-valid JSON on at least ~90% of first attempts, since the whole pipeline depends on structured output.
**Trade-off:** Expected recall is lower than a frontier-model implementation would achieve, and rate limits constrain how often the eval harness can run. Both are accepted and reported honestly — the evaluation names the model that produced each number. This is consistent with the project's stated positioning, which does not claim to out-perform funded competitors.

## ADR-004 — No Vector Database / RAG in v1
**Decision:** Use targeted code search for connected-file context, not embeddings-based retrieval.
**Rationale:** At the repo sizes this project targets, direct reference search is sufficient; adding a vector store would be complexity without a corresponding need.
**Trade-off:** Less thorough than a full code-graph approach (Greptile's); documented as a deliberate v1 scope limit, with a deeper graph planned for v2 only if targeted search proves insufficient.

## ADR-005 — Comments Only, No Autofix or Auto-Merge Blocking
**Decision:** The tool posts review comments; it never writes code or blocks a merge.
**Rationale:** Keeps the human as the final decision-maker and avoids the much higher stakes/scope risk of autofix in a portfolio-timeline project.
**Trade-off:** Less "impressive-looking" than an autofix demo, but lower risk and more honest about v1 scope.

## ADR-006 — Batch Classifier and Judge Calls to Conserve Request Quota
**Context:** Free-tier providers meter usage per *request*, not per token. A naive implementation issues one classifier call per file and one judge call per finding — roughly 20 API calls for a typical 5-file PR, and ~800 calls for a single 40-PR evaluation run, which can exceed a day's quota.
**Decision:** Batch the two mechanical stages. Classify all changed files in a single call, and judge all findings for a file (or for the whole PR) in a single call. Keep review generation one call per file.
**Rationale:** Classification is short-output tagging and batches with no quality loss. Judge calls currently re-send the same file context once per finding, so batching removes pure duplication. This cuts a typical review from ~20 requests to ~7, roughly tripling how many evaluation runs fit inside a daily quota — which directly determines how many tuning iterations are possible in weeks 6-7. Review generation is deliberately *not* batched, because focused per-file context is where review quality comes from.
**Trade-off:** Batched prompts are longer and their structured output is more complex to validate; a single malformed response now costs several files' worth of results rather than one.
