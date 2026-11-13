# The Review Pipeline in Detail

`06_HLD.md` says what the stages are. This says how each one works, what
stops it, and what it costs.

## Why agents at all

A reviewer that reads the diff and answers in one call can only use context
assembled up front — and that context is assembled by guessing what will
matter before anyone knows what the bug is.

Some bugs cannot be seen that way. A renamed dictionary key breaks the file
that reads it, and that file references a string, not a function — so symbol
and import analysis never retrieves it. The only way to find it is to go and
look, which means deciding what to look at *after* reading the diff.

That is the whole argument for a loop: not that models are better at
reviewing in a loop, but that a reviewer which cannot ask a follow-up
question is blind to an entire class of bug.

## The tool loop

`src/agents/loop.py`. Send the conversation; the model replies with either
tool calls or an answer; run the tools, append what they said, send again.

```
system + diff + context ──► model ──► tool calls ──► toolbox
                              ▲                          │
                              └──────── results ◄────────┘
                              │
                              └──► answer ──► findings
```

Every turn resends the whole conversation, so cost grows with the square of
the turn count. That is what the caps are for.

### Stopping

A run ends for one of five reasons, and records which:

| Reason | Meaning |
|---|---|
| `answered` | the model replied with findings |
| `turn cap` | six turns used |
| `token cap` | 80,000 tokens for this one agent |
| `no new information` | every call in a turn repeated an earlier one |
| `model error` | the provider refused |

The last three do not end the run empty-handed. Whatever the agent has
already read is still in the conversation, so it is asked once more for a
conclusion — with no tools offered, because a model shown lookups it has been
told not to use will reach for one anyway and never answer.

### Caps

| Cap | Value | Why |
|---|---|---|
| `MAX_TURNS` | 6 | hard stop on an unbounded loop |
| `MAX_TOOL_CALLS_PER_TURN` | 5 | stops a model asking for forty greps at once |
| `MAX_TOKENS` | 80,000 | per agent, not per review |
| `MAX_CANDIDATES_PER_SPECIALIST` | 4 | stops one noisy agent flooding verification |
| `LAST_CALL_ATTEMPTS` | 2 | asking for the conclusion, twice at most |
| `MAX_TOOL_NAME_CORRECTIONS` | 2 | a model that invents a third tool name will not be talked round |

The token cap is **per agent**. A single per-review cap would trip during the
specialists on a large pull request, and verification — the stage that
decides precision — would never run.

Repeat detection hashes `(tool name, arguments)`. A turn in which every call
repeats an earlier one means the model is circling, not closing in.

## The toolbox

`src/agents/tools.py`. The same functions `src/context/` already uses; the
difference is who decides when to call them.

| Tool | Returns | Cap |
|---|---|---|
| `read_file` | a numbered window of a file | 200 lines |
| `grep` | literal substring matches, optionally globbed | 40 matches |
| `get_definition` | where a symbol is defined | 10 definitions |
| `find_references` | uses of a symbol, with confidence scores | 25 references |
| `list_tests` | tests that touch a path | — |

Every tool returns a **string**, because that is what goes back into the
conversation. Line numbers are included so the model can cite them; paths are
repo-relative so it can pass them straight to another tool. Lines are
truncated at 300 characters.

Every tool is **bounded**, and says so when it truncates. One careless answer
on an 8,000-tokens-per-minute budget costs the whole review.

A bad argument comes back as text the model can correct from. A tool name
that does not exist is rejected by the provider before the call reaches us,
so the loop names the five real tools and lets the model try again.

## Triage

`src/agents/triage.py`. **No model call.** The classifier has already labelled
every file, so routing is a lookup over labels already paid for.

| Label | Wakes |
|---|---|
| logic, api, schema | both |
| config, dependency | wrong |
| test | robust |
| docs | nobody |

A docs-only pull request costs nothing past classification.

## The specialists

Two, not four. A narrow prompt makes a model dig and reach for its tools; a
prompt asking for "any problems" spreads attention thinly. That gain is real
at two. There is no evidence it keeps compounding, and each extra specialist
is another full pass over the diff.

| Specialist | Scope |
|---|---|
| **wrong** | correctness and security |
| **robust** | error handling and concurrency |

They run concurrently and start from the pre-built context rather than cold.
Turn one costs more because the diff and the reference table are already in
it, but the loop converges sooner, and one larger prompt paid once beats more
turns that each resend everything.

Test coverage is deliberately not a specialist. "Does a test touch this
changed function?" is a path and import lookup, so it runs as a tool and
arrives in the prompt as a fact.

### What the prompts enforce

- A finding must sit on a line that appears in the diff, because a comment
  can only attach to a changed line. A problem in an untouched file is
  anchored on the change that caused it, with the other end named.
- Severity is graded by outcome: a crash on a reachable path is high
  however small the fix. Only high and medium are ever posted.
- A contract change — a signature, a return shape, a key, an argument count
  — must be chased across the whole repository, frontend included, because
  `find_references` follows imports and the other end is often in another
  language.
- Returning no findings after checking properly is a correct answer.

## Verification

Two paths, because verification carries most of the per-review cost.

| Finding | Path |
|---|---|
| high severity, or confidence below 0.75 | its own tool loop, one per file |
| everything else | one cheap call |

Candidates cluster in the files the pull request touched and their evidence
overlaps, so the loop batches **per file, not per candidate** — opening a file
once instead of once per finding.

A verdict is `supported`, `contradicted` or `unverifiable`. Unverifiable is
kept but multiplied by 0.7, so a strong finding still clears the 0.6 gate and
a middling one does not. A missing verdict counts as unverifiable, never as
support. Both paths fail open: a verifier that errors leaves its findings
untouched, because silently discarding real findings is worse than keeping a
weak one.

Verification is the best-shaped stage for a loop. It arrives with a specific
claim to test, so it converges in a few turns. An agent told to "find bugs"
has no natural stopping point.

## Gates

| Gate | Runs | Drops |
|---|---|---|
| repeats | before verification | anything posted on an earlier commit |
| severity | before verification | low severity, which can never post |
| confidence | after verification | below 0.6, the score the judge just moved |

Repeats and severity run first so nothing is ever verified that could not be
posted. Repeats run *after* the gate rather than before, because a finding
held back last time was never seen by anyone — saying it now is new.

## What one review costs

Measured once, on a real pull request with four changed files
(`kaur-devs/codebase-chat#11`, 12 November 2026):

| | |
|---|---|
| Model calls | 18 |
| `wrong` | 8 turns, 35,621 tokens |
| `robust` | 7 turns, 28,895 tokens |
| Wall clock | 428s |

At Groq's list prices that is roughly **$0.017**, or **$0.035** when a
high-severity finding wakes the agentic verifier.

Three caveats, stated because the number will otherwise be quoted as
settled:

- **One data point.** Both specialists hit the turn cap, so this is a
  ceiling, not an average.
- **Nearly all of the 428 seconds was waiting**, not thinking. The free tier
  allows 8,000 tokens a minute; this review wanted 65,000.
- **It predates the token reservation** in `src/llm/budget.py`, which should
  cut the waiting sharply. Re-measure before trusting any of it.

`src/agents/measure.py` exists to produce these numbers but currently
bypasses the graph, so it undercounts. Fix it before the next measurement.

## What is not built

- **A fixer agent.** Suggestions come from the generating model as a
  replacement line, gated at 0.8 confidence and checked by compiling the
  file. No separate agent writes them.
- **Suppression.** Learning that a repository dismisses a category and
  falling silent on it.

## Decisions

The LangGraph choice and the two-model split are recorded in `21_ADRs.md`
(ADR-011 and ADR-003). This document describes what was built; that one
says why, and what was rejected.
