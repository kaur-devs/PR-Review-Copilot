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
PR opened/updated → webhook received → diff fetched → connected-file context gathered → change classified → task-decomposed review generated → judge/grounding pass → confidence/dedup filter → severity-gated comments posted → outcome captured → (v2) per-repo tuning.

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
- Autofix / one-click apply.
- Multi-platform support (GitLab, Bitbucket, Azure DevOps).
- Custom team rules / compliance configuration UI.
- Dedicated security-scanning pillar (SAST).
- IDE integration.
- Training or fine-tuning a model.
- Full-codebase graph indexing (v1 uses targeted search, not a full graph).
