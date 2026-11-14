# Traceability Matrix

Each row ties a business need to the requirement, the code that implements
it and the tests that hold it. "Component" names a module, not an endpoint:
the service has two routes, and everything else is reached through them.

| Business | Product | Technical | Component | Data | Tests |
|---|---|---|---|---|---|
| BR-001 | App installation | TR-001 | `src/github/auth.py` | installations | `integration/test_github_auth.py` |
| BR-002 | Webhook detection | TR-001 | `src/webhooks/` | webhook_events, reviews | `database/test_pipeline.py` |
| BR-003 | Diff fetch and parse | TR-002 | `src/diff/` | reviews | `unit/test_diff*.py` |
| BR-004 | Connected-file context | TR-003 | `src/context/` | — | `unit/test_languages.py` |
| BR-005 | Finding generation | TR-004/005 | `src/agents/specialists.py` | findings | `integration/test_specialists.py` |
| BR-006 | Verification | TR-006 | `src/judge/` | findings | `integration/test_judge.py` |
| BR-007 | Gates and repeats | TR-007 | `src/filter/` | findings | `unit/test_repeat.py`, `unit/test_candidate_caps.py` |
| BR-008 | Comment posting | TR-008 | `src/github/review.py` | reviews, findings | `database/test_review_posting.py` |
| BR-009 | Outcome capture | TR-009 | `src/outcomes/` | outcomes | `database/test_outcomes.py` |
| BR-010 | Evaluation | TR-010 | `src/evaluation/` | — | `unit/test_eval.py` |
| BR-011 | Bounded tool loop | TR-011 | `src/agents/loop.py` | — | `integration/test_loop.py` |
| BR-012 | Token budgets | TR-012 | `src/llm/budget.py` | — | `integration/test_provider.py` |
| BR-013 | Resume after a restart | TR-013 | `src/recovery.py` | reviews | `database/test_recovery.py` |
| BR-014 | Retire a superseded review | TR-014 | `src/db/queries.py` | reviews | `database/test_pipeline.py` |
| BR-015 | Multi-language parsing | TR-015 | `src/context/languages.py` | — | `unit/test_languages.py` |
| BR-016 | Suppress an ignored category | TR-017 | `src/filter/suppress.py` | outcomes, findings | `database/test_suppression.py` |
| BR-017 | Per-repository settings | TR-018 | `src/config_file.py` | — | `unit/test_repo_config.py` |
| BR-018 | Linter pass | TR-019 | `src/analysis/lint.py` | findings | `unit/test_lint.py` |
| BR-019 | Provenance and cost | TR-020 | `src/db/queries.py` | findings, reviews | `database/test_outcomes.py` |
| BR-020 | Deployment | TR-016 | — | — | **not built** |

The previous version of this matrix pointed BR-010 at `GET /eval-report`,
an endpoint that was specified and never written. Rows name modules now,
so a reader can open the thing being described.
