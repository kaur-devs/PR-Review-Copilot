# PR Review Copilot

An AI code reviewer that installs as a GitHub App. When you open a pull
request, it reads the change, looks up the other files affected by it, and
posts review comments.

## How the pieces fit together

A pull request arrives, and the work flows through `src/` in this order:

| Package | What it does |
|---|---|
| `webhooks/` | Receives the event from GitHub and checks it is genuine |
| `github/` | Proves who we are, fetches code, posts comments |
| `diff/` | Works out which lines changed |
| `context/` | Finds other files affected by those lines |
| `classification/` | Labels what kind of change this is |
| `review/` | Asks a language model for findings |
| `judge/` | Throws away findings the code does not support |
| `filter/` | Drops weak findings and duplicates |
| `outcomes/` | Records whether developers found the comments useful |
| `db/` | Stores events, reviews and findings |
| `common/` | Small helpers used by more than one package |

## Getting set up

You need Python 3.12 or newer and PostgreSQL running locally.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

Copy the example settings and fill them in:

```bash
cp .env.example .env
```

Create the database tables:

```bash
.venv/bin/alembic upgrade head
```

## Running it

```bash
.venv/bin/uvicorn src.main:app --reload --port 8000
```

GitHub needs a public address to send events to, so during development a
proxy forwards them to your machine:

```bash
npx smee-client --url https://smee.io/YOUR_CHANNEL --target http://localhost:8000/webhooks/github
```

## Tests

```bash
.venv/bin/python -m pytest
```

## Looking at the database

```bash
scripts/db tables
```

```bash
scripts/db neon tables
```

## Docs

Design documents live in `docs/`. `spikes/` holds throwaway experiments that
are kept for reference but are not part of the running service.
