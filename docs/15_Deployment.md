# Deployment

**Not deployed.** There is no `Dockerfile`, no `render.yaml`, no
`Procfile`. The service runs with `uvicorn` on a laptop, reached through a
tunnel, and the GitHub App's webhook URL points at that tunnel.

This is the one requirement that is binary rather than a matter of degree,
and it is the gap between a reviewer that works and a product somebody
else can install. TR-016 tracks it.

## What is actually running today

| | |
|---|---|
| Process | `uvicorn src.main:app --reload --port 8000` |
| Reachable at | a tunnel, re-pointed whenever it restarts |
| Database | Neon, hosted, shared with development |
| Models | Groq, Developer plan, 250K tokens/min per model |
| Secrets | `.env`, gitignored; the App key as a file path |

## What deploying it has to solve

These are known from running it, not guessed:

- **The private key.** `src/config.py` takes a *path* to the App's key.
  A hosted service has no file to point at, so the key has to arrive as an
  environment variable and be written out or loaded in memory.
- **Idle database connections.** Neon closes them. This has already caused
  one failure mid-review, fixed by holding short sessions around each
  phase rather than one across the whole review. A long-lived process will
  meet the same thing differently.
- **Recovery on startup.** `start_recovery()` scans for interrupted
  reviews every time the process boots. On a platform that restarts
  containers routinely, that runs far more often than it was designed for.
- **The webhook URL.** Changing it means updating the App settings, and
  deliveries sent to the old tunnel are lost rather than retried.
- **One container, one rate limiter.** The token budget is per process.
  Two instances would each believe they have the whole 250K per minute.

## No GPU, by constraint

All inference is through hosted APIs on a free or low-cost tier. Nothing
is fine-tuned or trained. The provider sits behind our own client, so
moving to another is four environment variables rather than a rewrite.

## Rollback

Not applicable until there is something to roll back. Schema changes are
versioned with Alembic and every migration has a tested `downgrade()`.
