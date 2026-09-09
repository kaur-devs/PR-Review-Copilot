# PR Review Copilot

## Problem
Small and mid-sized engineering teams merge code without dedicated senior review capacity, so bugs a careful review would catch ship to production instead.

## Solution
An installable GitHub App that reads a pull request's diff and connected code context, generates targeted findings, verifies them before posting, and gets quieter over time about what a specific team doesn't care about.

## Key Features
- GitHub App installation
- Webhook-triggered diff ingestion
- Connected-file context retrieval
- Task-decomposed review generation
- Judge/grounding pass
- Confidence + dedup filtering
- Severity-gated inline PR comments
- Outcome capture
- Evaluation harness

## Architecture
GitHub webhook → FastAPI backend → Diff Fetcher + Context Gatherer → Change Classifier → Review Generator → Judge Pass → Confidence/Dedup Filter → Comment Poster → Postgres.

## Tech Stack
Python, FastAPI, PostgreSQL, GitHub App API, free-tier LLM provider APIs behind a provider-agnostic adapter.

## Important Disclaimer
This is a student portfolio project — a transparent, scoped implementation of proven techniques from funded competitors (Greptile, CodeRabbit, Ellipsis), built to demonstrate applied LLM-evaluation engineering. It does not claim to outperform those tools and is not an autonomous merge gate; a human developer remains responsible for the final merge decision.

## Running
Document GitHub App credentials and LLM provider API keys as environment variables. Never commit secrets.
