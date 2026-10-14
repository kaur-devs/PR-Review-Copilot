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
**Decision:** Run every pipeline stage on free-tier hosted models, accessed only through a provider-agnostic adapter speaking the OpenAI-compatible chat completions format. Paid model tiers are explicitly out of scope.

**Provider chosen: Groq**, 14,400 requests per day with no card. Candidates considered and rejected: OpenRouter (50 requests per day without credits, too tight for the evaluation harness), Cerebras (**dropped its permanent free tier in July 2026**; now a $5 credit requiring a payment method), Gemini (usable as a fallback, allocation varies per model).

**Model: `openai/gpt-oss-120b`.** Groq retired the Llama models in August 2026, so the earlier plan of a Haiku/Sonnet-style split does not apply. Three models on the free tier have usable context windows: `openai/gpt-oss-120b`, `openai/gpt-oss-20b` and `qwen/qwen3.8-27b`. All three returned schema-valid JSON on the first attempt when tested.
**Rationale:** Free tiers are capable enough for this pipeline, and the adapter makes the provider a configuration choice rather than an architectural commitment — so a provider can be swapped when rate limits or output quality demand it. Provider selection is empirical: a candidate is rejected if it cannot return schema-valid JSON on at least ~90% of first attempts, since the whole pipeline depends on structured output.
**Trade-off:** Expected recall is lower than a frontier-model implementation would achieve, and rate limits constrain how often the eval harness can run. Both are accepted and reported honestly — the evaluation names the model that produced each number. This is consistent with the project's stated positioning, which does not claim to out-perform funded competitors.

**Measured since:** the binding limit is **tokens per minute (8,000), not requests per minute**. One review carrying its connected-file context is most of a minute's budget, so the harness paces against the provider's own rate-limit headers (see ADR-009).

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

## ADR-007 — The Judge Abstains Rather Than Deleting What It Cannot Check
**Context:** A second model verifies each finding against the code before it is stored. Originally the verdict was binary: supported, or rejected and discarded.

**Observed failure:** on a pull request that renamed a dictionary key, the generator correctly reported that callers would break. The judge rejected it, reasoning that "no code in the provided repository context accesses the `code_text` key". That was true of the evidence it had — the connected-file stage had retrieved one caller, and the lines proving the breakage had been trimmed out of the snippet. A correct finding was deleted, confidently, and the developer never saw it.

**Decision:** the verdict is three-way. `supported` keeps the finding. `contradicted` deletes it, and is only valid when the code shown positively disproves the claim. `unverifiable` keeps the finding with its confidence multiplied by 0.7.

**Rationale:** absence of evidence is not evidence of absence, and a judge shown a fraction of a repository is frequently in that position. The penalty interacts with the posting floor of 0.6 so the response is graduated: a finding at 0.97 still reaches the developer at 0.68, one at 0.80 drops to 0.56 and is stored but not posted.

**Trade-off:** more weak findings survive than under a binary judge. That is the intended direction — a reviewer that silently deletes true findings is worse than one that occasionally posts a weak one, because the first failure is invisible.

## ADR-008 — Context Snippets Are Trimmed Around the Reference, Not From the Top
**Context:** A connected file is included as the function enclosing the reference, capped at 60 lines.

**Observed failure:** the caller function was 86 lines. Trimmed from its first line, the snippet contained the setup and stopped one line before the code that read the renamed keys. The judge then correctly reported it could not see the breakage.

**Decision:** when a function exceeds the cap, centre the window on the reference line rather than starting at the function's first line, and mark how many lines were dropped on each side.

**Rationale:** what a reviewer needs is the code around the reference, and especially what happens to a returned value afterwards. Trimming from the top reliably keeps the least useful part.

**Measured:** the affected case went from 1/2 to 2/2 found, repeated across three runs.

## ADR-009 — Pace Against the Provider's Token Budget, Not a Fixed Backoff
**Context:** Free tiers cap tokens per minute as well as requests. Groq's limit is 8,000 TPM, and a review carrying its connected-file context is roughly 7,000 characters of prompt.

**Observed failure:** a fixed backoff of 2 then 8 seconds retried straight back into the same closed one-minute window. One of four evaluation cases could not complete, and was scored 0/2 as though it had failed on capability.

**Decision:** obey `retry-after` when the provider sends it, and read `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens` from every response to pause *before* a request that will not fit. Waits are capped at 90 seconds and the time spent waiting is counted.

**Rationale:** the provider knows exactly how long its window has left; guessing is how a retry lands inside the same closed window. Pausing is faster than being refused and retrying.

**Trade-off:** evaluation runs are slower. A full ablation sweep is bounded by the token window rather than by compute, which is the concrete reason the project would benefit from funded inference.

## ADR-010 — Store Findings Below the Posting Threshold
**Context:** Findings are gated by severity and confidence before posting.

**Decision:** every finding that survives the judge is written to the database, including those the gate holds back. `posted` records whether it reached the developer.

**Rationale:** thresholds can then be tuned by querying stored data rather than re-running the pipeline. Asking "what would precision have been at a floor of 0.5?" becomes a SQL query costing nothing, instead of an experiment costing a day of rate-limited model calls. On a free tier that difference decides how many tuning iterations fit in the schedule.

**Trade-off:** the findings table grows faster, and a reader must remember that a row's existence does not mean a developer ever saw it.
