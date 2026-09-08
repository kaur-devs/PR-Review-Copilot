# Testing Strategy

## Test Levels
1. Unit
2. Integration
3. API/webhook
4. End-to-end (real PR against a test repo)
5. Security
6. Evaluation (recall / false-positive rate)

## Sample Test Cases
| ID | Scenario | Expected Result | Priority |
|---|---|---|---|
| TEST-001 | Valid PR opened webhook | Review pipeline triggers | High |
| TEST-002 | Invalid webhook signature | Request rejected, no processing | High |
| TEST-003 | Empty diff | Skipped, no findings generated | Medium |
| TEST-004 | Binary file in diff | Skipped from review | Medium |
| TEST-005 | Malformed LLM output | Rejected by schema validation, retried once | High |
| TEST-006 | Judge pass given an ungrounded finding | Finding discarded before posting | High |
| TEST-007 | Duplicate findings on the same block | Merged into one comment | Medium |
| TEST-008 | GitHub API rate limit hit | Backoff/retry, review not silently dropped | High |
| TEST-009 | Duplicate webhook delivery (GitHub retry) | Idempotent — no duplicate comments posted | High |
| TEST-010 | LLM API unavailable | Review fails gracefully, logged, not crashed | High |

## Evaluation (recall / false-positive rate)
Curate 30-50 real historical PRs from open-source repos with a documented follow-up bugfix commit (a bug the original PR missed) or a clean merge (a true negative). Run the pipeline against each and report recall and false-positive rate explicitly, calibrated against the competitor benchmarks in the GenAI Architecture document.
