# Architecture Decision Records

## ADR-001 — GitHub App, not OAuth App or Personal Access Token
**Context:** The reviewer needs to act on repos it doesn't own, with scoped permissions.
**Decision:** Register a GitHub App with minimal permissions (PR read/write, contents read).
**Rationale:** A GitHub App has its own identity and per-installation permission scoping; a PAT ties to a personal account and can't be distributed the same way.
**Trade-off:** More setup complexity than a quick PAT-based script.

## ADR-002 — FastAPI Backend, No Agent Framework
**Superseded by ADR-011.**
**Decision:** Orchestrate the pipeline with direct API calls in FastAPI, not a heavy agent framework.
**Rationale:** The pipeline is a fixed sequence (fetch → classify → generate → judge → filter → post), not an open-ended agent loop — a framework would add abstraction without solving a real problem here.
**Trade-off:** Some boilerplate that a framework might otherwise handle.
**Why it no longer holds:** the premise was that the pipeline is a fixed sequence. Two reviewers now run concurrently and a verifier loops, so the sequence branches and resuming it part-way is worth something. ADR-011 re-examines the choice on those terms.

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

## ADR-006 — Batch the Mechanical Stages
**Context:** a naive implementation issues one classifier call per file and one judge call per finding. On a five-file pull request with ten findings that is roughly twenty calls, each resending the same system prompt, the same tool schemas and much of the same file context.

**Decision:** batch the two mechanical stages. Classify all changed files in one call; judge all findings for a review in one call. Do not batch review generation, where focused per-scope context is where quality comes from.

**Rationale:** classification is short-output tagging and batches with no quality loss. Judging re-sent the same context once per finding, which was pure duplication. Both stages now cost one call each regardless of size.

**Corrected premise.** This decision was originally argued on *request* quota, on the belief that free tiers meter per request. They do not: Groq meters tokens per minute and tokens per day against each model id, and both walls this project has hit were token walls. The decision survives the correction, but for a different reason — what batching saves is the per-call overhead of resent prompts and schemas, measured in tokens, not a request count that was never binding. Full numbers in the quota analysis.

**Trade-off:** batched prompts are longer and their structured output is harder to validate; one malformed response now costs every finding in the review rather than one.

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


## ADR-011 — LangGraph for Flow Between Stages
**Context:** The review became two concurrent reviewers, a conditional verifier and three gates. `src/pipeline/record.py` had to be rewritten for that regardless of what orchestrated it.

**Decision:** Define the flow as a LangGraph `StateGraph`. **LangGraph owns the flow between stages; our code owns what happens inside a stage, and every HTTP call to a model.**

**Rationale:** Measured marginally, against the rewrite that was happening anyway, the two routes cost the same: ~625 lines by hand against ~620 with LangGraph. The hand-rolled route spends its share on a candidates table, a migration and save/load queries — checkpointing written by us at two chosen points. LangGraph checkpoints after every node instead. Once cost is a wash the tie-breakers decide: finer-grained recovery, a flow readable in fifteen declarative lines rather than a hundred and sixty imperative ones, and a framework a reviewer expects to have been evaluated.

**What was rejected:** `create_react_agent`. It needs a LangChain model object, and `ChatGroq` retries *after* a 429 rather than waiting before one. On an 8,000-tokens-per-minute budget that is the difference between working and failing, so `src/llm/` stays ours.

**What the checkpoint holds:** decisions, never resources. The repository snapshot and the open HTTP clients are passed in at invoke time. They are derived from the head SHA, so a resumed run rebuilds them; checkpointing them would store a temporary directory that no longer exists.

**Trade-off:** a dependency with its own release cadence, pinned exactly, on a flow simple enough to have been written by hand.

## ADR-012 — Two Specialists, Not Four
**Context:** A narrow prompt makes a model dig and reach for its tools; a prompt asking for "any problems" spreads attention thinly.

**Decision:** Two specialists — **wrong** (correctness, security) and **robust** (error handling, concurrency). Test coverage is a tool, not an agent.

**Rationale:** The gain from narrowing is real at two. There is no evidence it keeps compounding, and each additional specialist is another full pass over the diff — the most expensive stage, doubled. "Does a test touch this changed function?" is a path and import lookup, so it runs as a tool and arrives in the prompt as a fact rather than a judgement.

**Trade-off:** two broad scopes rather than four narrow ones. If measurement later shows one specialist's findings cluster in a sub-area, splitting it is a prompt change, not an architecture change.
