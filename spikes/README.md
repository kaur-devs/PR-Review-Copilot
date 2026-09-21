# Spikes

Throwaway experimental code. Nothing here is imported by `src/`, and nothing
here should be copied into `src/` unchanged.

## context_spike.py

Runs the connected-file context pipeline end to end against a local fixture,
with no GitHub calls and no LLM calls.

```bash
.venv/bin/python spikes/context_spike.py
```

### Stages

| Stage | Does | Uses |
|---|---|---|
| 1 Parsed diff | Unified diff to changed line numbers | `unidiff` |
| 2 Snapshot | Repo contents at one commit | Local directory here, Contents API tarball in production |
| 3 Symbol resolver | Changed lines to enclosing definitions | `tree-sitter`, parent walk |
| 4 Candidate finder | Literal byte scan for the symbol name | No parsing at all |
| 5 Reference verifier | Confirms genuine call sites, checks the import edge | `tree-sitter` queries |
| 6 Bundle assembler | Merge, rank, cut to budget, attach provenance | Own code |

### The fixture and its traps

`fixture/` is five small Python files with a known answer. `sample.diff`
changes `get_user` in `models.py` from raising `KeyError` to returning `None`,
so every caller that assumed a non-None result is now affected.

| File | What it is | Expected |
|---|---|---|
| `service.py` | Imports and calls `get_user` twice | retrieved |
| `reports.py` | Imports it aliased as `fetch_user` | retrieved |
| `billing.py` | Name appears only in a docstring, a comment and a log string | dropped |
| `notifications.py` | Calls `client.get_user`, a different symbol sharing the name | dropped |

`reports.py` is the interesting one. A call-site search for `get_user` finds
nothing there, because the call reads `fetch_user(...)`. It is recovered by
reading the `aliased_import` node and re-running the search under the local
name. This is the case that justifies parsing over grepping.

### Known limits, by design

- One language. Every query assumes the Python grammar.
- No type inference, so `obj.get_user()` can only ever be a guess.
- `a.b.get_user()` is not matched; the attribute query expects an identifier
  object.
- Outbound definitions are found by name only, with no scope resolution.

### Two things carried over to production deliberately

- `Snapshot` has the interface the tarball reader will have, so only the
  constructor changes.
- `ensure_diff_headers` exists because GitHub's Files API `patch` field omits
  the `---`/`+++` headers that `unidiff` requires. `git diff` includes them, so
  this is a no-op locally and load-bearing in production.
