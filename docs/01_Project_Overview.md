# PR Review Copilot — Project Overview

## Project Identity
- **Track:** Generative AI
- **Domain:** Developer Tools / Software Engineering
- **Duration:** 8 weeks
- **Team:** 1 student
- **Skill level:** Intermediate to Advanced
- **Primary users:** Engineering teams (2-15 developers), individual developers reviewing their own repos
- **Constraint:** No local GPU and no paid-API budget — orchestration-only, free-tier hosted LLM APIs

## Executive Summary
PR Review Copilot is an end-to-end AI code review system that reads a pull request's diff, gathers the surrounding code context it actually touches, generates targeted findings, verifies each finding against the real code before it's shown to anyone, and posts the result as real GitHub review comments.

The platform is intentionally designed as a **review-support system, not an autonomous merge gate**. A human developer remains responsible for the final decision to merge.

## Core Product Flow
PR opened or updated → webhook verified and the commit claimed → diff
fetched and parsed → repository snapshot downloaded → change classified →
triage picks which reviewers to wake → **one agent per changed file, per
reviewer, each able to grep and read the repository while it decides** →
a linter pass on the same changed lines → repeats dropped → severity gate
→ verification by a second model from a different family → confidence
gate → the repository's own settings and any suppressed categories →
severity-gated inline comments posted → outcome captured.

The single-pass version of this flow — classify, generate, judge, filter
— still exists and is what the pipeline falls back to. It was replaced as
the default because it could not find a defect whose two ends sat in
different files: the context it needed had to be chosen before anyone
knew what the bug was.

## MVP
1. GitHub App installable on a repo.
2. Webhook-triggered diff ingestion.
3. Lightweight connected-file context retrieval.
4. Task-decomposed review generation (targeted checks per change type).
5. Judge/grounding pass to discard ungrounded findings.
6. Confidence and duplicate filtering.
7. Severity-gated inline PR comments.
8. Outcome capture (accept/reject signal).
9. Evaluation harness (recall / false-positive rate on real historical PRs).

## Out of Scope for MVP
- Autofix, merging or blocking — out of scope by principle, not by effort.
- Multi-platform support (GitLab, Bitbucket, Azure DevOps).
- A configuration UI. Settings are a file in the repository instead.
- A dedicated SAST pillar. A curated linter pass runs, chosen rule by
  rule; a full security scanner is not in scope.
- IDE integration.
- Training or fine-tuning a model.
- Full-codebase graph indexing. Cross-file questions are answered by an
  agent at review time rather than by a pre-built index — the trade is
  flexibility against consistency, and the inconsistency is measurable.
- Chat on the pull request.
