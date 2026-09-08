# Business Requirements Document (BRD)

## 1. Executive Summary
Small and mid-sized engineering teams merge code without dedicated senior review capacity, so bugs a careful review would catch ship to production instead. The proposed system reduces this gap by automatically reviewing every PR for cross-file-aware, verified findings, without the infrastructure weight of the heaviest competing tools.

## 2. Problem Statement
Reviewers are busy, review queues back up, and existing AI code-review tools split across unresolved trade-offs — diff-only tools miss cross-file breakage, full-context tools require heavy graph infrastructure, and general-purpose tools generate false-positive fatigue, the #1 stated developer complaint about the category.

## 3. Vision
Build a transparent, installable AI reviewer that understands what a change actually touches, double-checks its own findings, and gets more useful over time by learning what a specific team cares about.

## 4. Objectives
- Reduce the "no one had time to review this" gap for small teams.
- Catch cross-file-breaking changes that diff-only review misses.
- Keep false-positive rate low enough that developers don't dismiss the tool.
- Produce a measured, honest evaluation (recall / false-positive rate), not just "it runs."
- Keep the entire pipeline transparent and buildable by one engineer, unlike closed competitor products.

## 5. Personas
| Persona | Goals | Pain Points |
|---|---|---|
| Engineering team lead (2-15 devs) | Ship code without shipping avoidable bugs | No dedicated senior reviewer for every PR |
| Individual developer | Get a second opinion on their own PR | Busy teammates, slow review turnaround |
| Builder (student) | Demonstrate applied LLM-evaluation engineering | Needs a credible, measured result, not just a demo |

## 6. Business Use Cases
- Install the GitHub App on a repository.
- Open or update a pull request.
- Receive targeted, grounded review comments automatically.
- Resolve or dismiss a comment (feeding the outcome/feedback loop).
- Review the evaluation report (recall / false-positive rate).
- View per-repo suppression behavior (v2).

## 7. Business Requirements
| ID | Requirement | Priority | Acceptance Criteria |
|---|---|---|---|
| BR-001 | App can be installed on a GitHub repository | Must | Installation completes via GitHub App flow |
| BR-002 | System detects PR open/update events | Must | Webhook received and signature verified |
| BR-003 | System fetches and parses the diff | Must | Structured hunks available per changed file |
| BR-004 | System gathers connected-file context | Must | Files referencing changed functions/classes are retrieved |
| BR-005 | System generates targeted findings per change type | Must | Findings include file, line, severity, category, rationale, confidence |
| BR-006 | System verifies findings before posting | Must | Ungrounded findings are discarded by the judge pass |
| BR-007 | System filters low-confidence/duplicate findings | Must | Only confident, deduplicated findings remain |
| BR-008 | System posts comments directly on the PR | Must | Comments appear inline on GitHub, severity-gated |
| BR-009 | System records comment outcomes | Must | Accept/reject signal stored per finding |
| BR-010 | System reports evaluation results | Should | Recall and false-positive rate available from a documented run |

## 8. Non-Functional Requirements
- Security: minimum-necessary GitHub App permissions; verified webhook signatures.
- Performance: a typical PR (≤8 files) should receive comments within roughly a minute.
- Reliability: transient LLM/API failures should not silently drop a review.
- Auditability: every posted finding retains its model, prompt-template, and confidence metadata.
- Cost: prefer the cheapest capable model per pipeline step; track dollars-per-review from week 1.

## 9. Success Metrics
- Recall against a curated set of real historical PR bugs.
- False-positive rate against the same set.
- Cost per PR review.
- Judge-pass grounding accuracy (how often the judge correctly discards a bad finding).
- Qualitative: would these findings have been worth a human reviewer's time (installed on the builder's own repos).

## 10. Risks
| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| False-positive fatigue | Medium | High | Judge pass + confidence filtering built into v1, not deferred |
| Hard to source real-world test data | Medium | Medium | Use open-source repos with documented bugfix-commit history instead of private team data |
| LLM API cost scales with PR volume | Low | Medium | Review only changed hunks + targeted context; Haiku/Sonnet split; cost tracked per review |
| Hosting cold-starts delay first review after inactivity | Medium | Low | Accepted tradeoff on free-tier hosting; documented, not hidden |
| Scope creep toward autofix/multi-platform/security scanning | Medium | Medium | Explicitly deferred to Version 3 with reasons documented |

## 11. MVP Scope
**In scope:** GitHub App install, webhook ingestion, connected-file context, task-decomposed review, judge/grounding pass, confidence/dedup filtering, severity-gated comment posting, outcome capture, evaluation harness.

**Out of scope:** autofix, multi-platform support, custom rules UI, dedicated security scanning, IDE integration, model training.

## 12. Future Scope
- PR-level summary comment.
- Feedback-driven per-repo suppression.
- Deeper cross-file dependency graph.
- Confidence scores surfaced in comments.
- Multi-platform support.
- Autofix suggestions.
