# Traceability Matrix

| Business | Product | Technical | API/Component | Data | Test |
|---|---|---|---|---|---|
| BR-001 | App installation | TR-001 | GitHub App install flow | installations | TEST-001 |
| BR-002 | Webhook detection | TR-001 | POST /webhooks/github | reviews | TEST-002 |
| BR-003 | Diff fetch/parse | TR-002 | Diff Fetcher | reviews | TEST-003/004 |
| BR-004 | Connected-file context | TR-003 | Context Gatherer | — | — |
| BR-005 | Finding generation | TR-004/005 | Review Generator | findings | TEST-005 |
| BR-006 | Judge/grounding pass | TR-006 | Judge Pass | findings | TEST-006 |
| BR-007 | Confidence/dedup filter | TR-007 | Filter | findings | TEST-007 |
| BR-008 | Comment posting | TR-008 | Comment Poster | findings | TEST-008/009 |
| BR-009 | Outcome capture | TR-009 | Outcome Tracker | outcomes | — |
| BR-010 | Evaluation report | TR-010 | GET /eval-report | — | evaluation |
