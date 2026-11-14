# CI/CD

## What actually runs

Nothing automated. There is no `.github/workflows/`, so every check below
is one somebody types.

```bash
.venv/bin/python -m pytest                 # 677 tests, ~18s
.venv/bin/ruff check src tests             # clean; the rules are in pyproject.toml
.venv/bin/python -m alembic upgrade head   # before anything touches the schema
.venv/bin/python scripts/build_docs.py     # regenerate the bundle
```

The ruff configuration is the same curated selector the reviewer applies
to other people's pull requests, so the project is held to its own rules.
It found eight things in this codebase the first time it was pointed at
it; they are fixed.

Describing a pipeline that does not exist was the previous version of this
document, which drew a diagram ending in "Deploy to Render (staging)".
Nothing has ever deployed to Render.

## What a pipeline would need to contain

In this order, because each step is cheap relative to the one after it:

1. `pytest` — the whole suite needs Postgres, so CI needs a service
   container and `TEST_DATABASE_URL` pointing at it. `conftest.py` refuses
   to run when that matches `DATABASE_URL`, which is what stops a
   misconfigured job wiping the real review history.
2. `ruff check` — the project now depends on ruff anyway, for the review
   pass itself.
3. `alembic upgrade head` against a throwaway database, so a migration
   that does not apply fails before it reaches anything real.
4. Build and deploy — blocked on TR-016, which is not built.

## Branching

`main` is stable. Work happens on `feature/*` and merges by pull request.

## Pull requests

Problem, solution, testing performed, risks. The same discipline the tool
asks of everyone else.

## Quality gates

Do not merge on a failing test or a detected secret. Both are currently
enforced by reading, not by tooling, which is the honest state of it.
