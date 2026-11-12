# Tests

625 tests. The whole suite runs in about 23 seconds.

```bash
.venv/bin/python -m pytest                    # everything
.venv/bin/python -m pytest tests/unit         # no Postgres needed
```

## Three folders, by what a test needs to run

| Folder | Needs | Covers |
|---|---|---|
| `unit/` | nothing | pure functions: diffs, gates, symbols, prompts, suggestions, languages |
| `integration/` | fake HTTP, no network | the model provider, the agent loop, specialists, the judge, GitHub clients |
| `database/` | Postgres (`TEST_DATABASE_URL`) | the webhook endpoint, the pipeline, the graph, persistence, recovery, outcomes |

The folder *is* the marker. `empty_tables` reads the path and skips the
truncate for anything outside `database/`, so `tests/unit` runs with no
database at all.

## Conventions

One test file per source module. `unit/test_gate.py` covers
`src/filter/gate.py`.

Test names are sentences, so a run reads as a list of behaviours:

    test_a_sound_suggestion_survives_the_whole_review
    test_a_low_severity_finding_never_reaches_the_verifier

A docstring on a test says *why the behaviour matters*, not what the code
does. Several name the live incident that produced them.

## Shared helpers

`fakes.py` builds fake GitHub and model servers over `httpx.MockTransport`,
so nothing reaches the network. `conftest.py` points the environment at the
test database and resets rate-limit state between tests.

## What this suite does not cover

Every test is single-threaded against fakes. The concurrency and
integration failures found in live running — a rate limiter that did not
reserve, a database session held across a ten-minute review — are not
reachable from here. That gap is real; the fix is to add those tests, not
to trust the green run further than it goes.
