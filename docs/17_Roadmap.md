# 8-Week Project Roadmap

| Phase | Weeks | Deliverables |
|---|---:|---|
| Foundation | 1 | GitHub App registration, webhook endpoint, signature verification, deploy skeleton |
| Diff & Context | 2 | Diff fetching/parsing, connected-file search, LLM provider adapter, free-tier provider trial (schema-validity gate) |
| Classification & Generation | 3 | Batched change classifier, task-decomposed prompt templates |
| Verification | 4 | Batched judge pass, confidence/dedup filtering, 8-PR mini-eval to validate provider viability |
| Posting & Tracking | 5 | Delivery recording, idempotent review claims, stuck-review recovery, comment posting, severity gating, outcome-tracking schema |
| Evaluation | 6 | Eval harness — curated PR set, recall/false-positive measurement |
| Tuning | 7 | Tuning against eval results, live test on own real repos |
| Ship | 8 | Documentation, architecture diagram, demo recording, deploy |

## Definition of Done
A feature is complete when: implemented, tested, documented, integrated, error paths handled, security considerations addressed, and demonstrable against a real PR.

## How it actually went

The plan held until week 5 and then changed shape, which is worth
recording rather than tidying away.

| Planned | What happened |
|---|---|
| Week 1: deploy skeleton | never built; the service still runs on a laptop behind a tunnel |
| Week 3–4: single-pass classify, generate, judge | built, then largely replaced |
| — | **the agentic rewrite**: two specialists with a bounded tool loop, then one agent per changed file |
| Week 6: eval harness | built in week 6, pointed at the wrong pipeline until much later |
| Week 7: tuning against eval results | tuning happened against live runs on real pull requests instead |
| Week 8: deploy | outstanding |

The rewrite came from a measurement: the single-pass reviewer could not
find a defect whose two ends sat in different files, because the context
it needed was chosen before anyone knew what the bug was. That is the
whole argument for the loop, and it is the thing the roadmap did not
anticipate.

Added along the way and not in the original plan: a linter pass, per-
repository settings, outcome-driven suppression, and provenance on every
finding.
