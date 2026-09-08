# Deployment Architecture

## Environments
Development → Staging (test repo) → Production/Demo

## Suggested Deployment
- FastAPI backend as a container/service on Render.
- PostgreSQL via Supabase (free tier).
- GitHub App registered against the deployed backend's public webhook URL.
- Claude API accessed via hosted endpoint — no local model hosting.

## No-GPU Constraint
Because no local/dedicated GPU is available:
1. All inference runs through hosted LLM APIs (Claude).
2. No local model fine-tuning or training is attempted.
3. Model access is kept provider-agnostic via an adapter, so a different hosted provider could be substituted without rearchitecting.
4. Cost and latency are tracked explicitly since API-based inference is the only path (see Cost Analysis).

## Rollback
Backend deployments are versioned; a broken deploy rolls back to the last known-good container image.

## Database Migration
Schema changes tracked with explicit migration scripts (e.g., Alembic) rather than ad hoc changes.
