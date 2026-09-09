# 8-Week Project Roadmap

| Phase | Weeks | Deliverables |
|---|---:|---|
| Foundation | 1 | GitHub App registration, webhook endpoint, signature verification, deploy skeleton |
| Diff & Context | 2 | Diff fetching/parsing, connected-file search, LLM provider adapter, free-tier provider trial (schema-validity gate) |
| Classification & Generation | 3 | Batched change classifier, task-decomposed prompt templates |
| Verification | 4 | Batched judge pass, confidence/dedup filtering, 8-PR mini-eval to validate provider viability |
| Posting & Tracking | 5 | Comment posting, severity gating, outcome-tracking schema |
| Evaluation | 6 | Eval harness — curated PR set, recall/false-positive measurement |
| Tuning | 7 | Tuning against eval results, live test on own real repos |
| Ship | 8 | Documentation, architecture diagram, demo recording, deploy |

## Definition of Done
A feature is complete when: implemented, tested, documented, integrated, error paths handled, security considerations addressed, and demonstrable against a real PR.
