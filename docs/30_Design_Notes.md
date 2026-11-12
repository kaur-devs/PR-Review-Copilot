# Design Notes

Why the code is the way it is. Each section was the opening docstring
of the module it names; they were moved here so a file starts with its
code rather than with an essay.


## `src/pipeline/agentic.py`

<a id="src-agentic_run"></a>

**Run one pull request through the agentic graph.**

The counterpart to `src/pipeline/single_pass.py`, which runs the single-pass
pipeline. Both produce a ReviewOutcome, touch neither the database nor
GitHub's comment API, and can be pointed at the same evaluation cases,
which is the only way to answer whether the agents are worth their cost.

Which one runs is a setting, not a code path chosen at build time:

    AGENTIC_PIPELINE=1

This module owns the resources the graph is not allowed to hold. The
snapshot is a temporary directory and the providers are open HTTP
clients; both are opened here, passed in at invoke time, and closed when
the review ends. A checkpoint that tried to store them would be storing
a path that no longer exists.


## `src/agents/loop.py`

<a id="src-agents-loop"></a>

**The loop that lets a model look things up before it answers.**

Send the conversation; the model replies with either tool calls or an
answer; run the tools, append what they said, send again. That is the
whole idea. Everything else in this file is about making sure it stops.

An unbounded loop is the real risk in an agentic design, not the cost of
any single turn. Published agent harnesses cap at fifty steps or more
because the failure mode is a long tail: a model that keeps re-reading
the same file, politely, until the budget is gone. Four separate limits
guard against that, and the loop records what it did so the turn count
can be measured rather than assumed.


## `src/agents/measure.py`

<a id="src-agents-measure"></a>

**Measure how many turns a specialist actually takes.**

Every cost and latency figure in docs/29 rests on one assumed number:
three turns per agent. Three was a guess. This runs the real agents
against real pull requests and counts.

The answer changes the plan rather than decorating it. At two turns the
free tier becomes comfortable; at six the design needs cutting back. So
this is deliberately a measurement and nothing else: it posts nothing,
writes nothing to the database, and runs no verifier.

    python -m src.agents.measure --cases eval/cases.json
    python -m src.agents.measure --repo owner/name --pr 42 --installation 123


## `src/agents/prompts.py`

<a id="src-agents-prompts"></a>

**What each specialist is told to look for.**

Two prompts, not one and not four. A prompt that asks for "any problems"
spreads the model's attention thinly across everything; a prompt that
says "you are only looking for injection" makes it dig and reach for its
tools. That gain is real at two. There is no evidence it keeps
compounding at four, and every extra specialist is another full pass
over the diff.

Test coverage is deliberately absent. "Does a test touch this changed
function?" is a path-convention and import-graph lookup, not a judgement
call, so it runs as a tool and arrives in the prompt as a fact.


## `src/agents/specialists.py`

<a id="src-agents-specialists"></a>

**Two reviewers, looking for different things, at the same time.**

Each one is an agent loop over the shared toolbox with its own narrow
prompt. They are independent, so they run concurrently and their
findings are concatenated — the thing LangGraph would call a reducer,
which at two agents is a list join.

Both start from the pre-computed context rather than cold. That is worth
more than it looks. Turn one costs more because the diff and the
reference table are already in it, but the loop converges in three or
four turns instead of eight, and the quadratic growth of a conversation
means one larger prompt paid once beats four more turns that each resend
everything. It also keeps the calibrated confidence scores in play
rather than replacing them with an agent's assertion.


## `src/agents/summary.py`

<a id="src-agents-summary"></a>

**The comment that orients a human reviewer.**

A reviewer opening a pull request today sees a handful of inline
comments and nothing else: no sense of what the change does, where the
risk sits, or what was looked at. Both reference designs rank this a
launch requirement, and it is the one feature of theirs this project
did not have.

Deliberately not an agent. The summary describes a change we are already
holding in full, so there is nothing to look up; a tool loop here would
spend turns confirming what is already in the prompt. One call.

It is also the only stage allowed to say something when nothing was
found. Silence reads as a broken bot.


## `src/agents/tools.py`

<a id="src-agents-tools"></a>

**What an agent is allowed to look at.**

These are the same functions `src/context/` already uses to build context
up front; the difference is who decides when to call them. The context
builder guesses what will matter before anyone knows what the bug is. An
agent asks because of what it already suspects.

Two rules shape everything here.

Every tool returns a **string**, because that is what goes back into the
conversation. Shape matters as much as content: line numbers are included
so the model can cite them, and paths are repo-relative so it can pass
them straight back to another tool.

Every tool is **bounded**. A grep over a large repository could return
thousands of lines, and on an 8,000 tokens-per-minute budget one careless
answer costs the whole review. Where output is cut, the tool says so,
rather than letting the model believe it saw everything.


## `src/agents/triage.py`

<a id="src-agents-triage"></a>

**Which specialists are worth waking for this pull request.**

This is the stage that controls the bill. A documentation-only change
should cost nothing, and a dependency bump should not pay two agents to
read a version number. Waking both specialists on every pull request
would roughly triple the cost of the system for no gain on the pull
requests that cannot contain the problems they look for.

It needs **no model call of its own**. `src/classification/` already
labels every changed file, using rules where the path is decisive and
the model only where it is not, so the routing here is a lookup over
labels that have already been paid for.


## `src/context/languages.py`

<a id="src-context-languages"></a>

**What the rest of the code needs to know about a programming language.**

Everything that differs between languages lives behind one object, so adding
a language is adding an instance rather than editing six files. The six
things that actually differ:

    the grammar            tree-sitter needs a different parser per language
    what a definition is   function_definition here, function_declaration there
    four queries           written against that grammar's node names
    import resolution      how a module name maps to a file path
    import node types      which parents mean "this is an import, not a use"
    validity               how to tell whether source is syntactically whole

A language is only registered once all six exist. A half-ported language is
worse than none, because the review looks identical while being blinder: no
symbols, no callers, and no way for the reader to tell.

Every language's IMPORTS query must use the same capture names - module,
imported, alias and module_only - because analyse_imports in references.py
reads captures by name and is deliberately language-agnostic.


## `src/db/models.py`

<a id="src-db-models"></a>

**The database tables.**

Two ideas drive the whole design.

First, every genuine delivery from GitHub gets a row, including the ones we
ignored and the ones we had already seen. If something goes wrong later, we
can account for every message GitHub sent us.

Second, a "review" means one commit on one pull request. Pushing new code
creates a new review. Reopening a pull request without pushing does not,
because the code has not changed.


## `src/db/queries.py`

<a id="src-db-queries"></a>

**Reading and writing rows.**

One idea runs through every function here: we never check whether something
exists and then write it. We let the database decide.

Checking first looks correct and is not. If two messages about the same
commit arrive at the same moment, both look, both see nothing, and both
write. You end up reviewing the same commit twice.

Instead we always try to write, and tell the database "if this clashes with
an existing row, do nothing". The database handles one request at a time, so
exactly one of the two wins. The loser gets told nothing was written, which
is how we know it was a duplicate.

In SQL that instruction is called ON CONFLICT DO NOTHING.


## `src/filter/repeat.py`

<a id="src-filter-repeat"></a>

**Not saying the same thing twice on one pull request.**

Every push creates a new head commit, which is a new review. Without this, a
developer who pushes a fix gets the whole previous review again on the lines
they did not touch, which is the single fastest way to have a bot uninstalled.

What counts as the same finding is the interesting decision. Not the message
text: the generator is not deterministic even at temperature zero, because
these models are served in batches, so the same bug comes back worded
differently between runs. Matching on wording would dedupe almost nothing.

So a repeat is the same category of problem, in the same file, at
approximately the same place. Approximately because a push shifts line
numbers: a finding on line 113 can be on 118 after three lines are added
above it, and it is still the same finding.


## `src/filter/suggest.py`

<a id="src-filter-suggest"></a>

**Deciding which findings may offer a replacement line.**

A wrong comment costs the reader ten seconds. A wrong suggestion costs them
one click, and the broken code is then theirs. So a suggestion passes a
stricter gate than the comment carrying it, and a refused suggestion never
hides the comment: the finding posts as prose instead.

The last two checks are the real ones. The suggestion is applied to the file
as it exists at the reviewed commit, and the result must both compile and sit
at the same indentation as the line it replaced.

Note that this does not use tree-sitter, which the rest of the project does.
tree-sitter is deliberately tolerant: it has to extract symbols from code that
may be broken, so it never raises. Here we want the opposite, and measurably
so. tree-sitter accepts both of these happily:

    def average(values):
        total = sum(values)
    return total / len(values)        <- de-indented out of the function

CPython does not: that is a SyntaxError, and an over-indented line is an
IndentationError. Different job, different tool.


## `src/github/auth.py`

<a id="src-github-auth"></a>

**Proving to GitHub that we are who we say we are.**

This happens in two steps, and understanding why there are two is worth a
minute.

Step one. Our App has a private key, which only we hold. We use it to sign a
short note saying "I am App number 5039285", valid for a few minutes. That
note is called a JSON Web Token, or JWT. Anyone can read it, but only someone
with our private key could have produced it.

That note proves we are the App. It does not let us read anybody's code.

Step two. We hand the note to GitHub and ask for a token for one particular
installation. GitHub checks the note, checks that installation really exists,
and gives back a token that works for one hour and can only touch the
repositories that installation was given.

Why bother with two steps? Because the second token is narrow and short-lived.
If it leaks, it expires within the hour and only ever reached one account. Our
private key, which never leaves this machine, stays safe.


## `src/pipeline/graph.py`

<a id="src-graph"></a>

**The agentic review, as a checkpointed state graph.**

One review is a fixed sequence of stages, two of which run concurrently.
That is a graph, and LangGraph saves the state after every node, so a
worker that dies mid-review resumes at the stage it reached instead of
starting again. At roughly a hundred thousand tokens a review, that is
the difference between a crash costing a cent and costing the lot.

    triage ──┬── wrong  ──┐
             └── robust ──┴── repeats ── severity ── verify
                                                       │
                       publish ── summary ── suggest ──┘

Two boundaries are deliberate and worth keeping.

**LangGraph owns the flow between stages. Our code owns what happens
inside one, and every HTTP call to a model.** `create_react_agent` is
not used: it needs a LangChain model object, and `ChatGroq` retries
after a 429 rather than waiting before one. On an eight-thousand token
per minute budget that distinction is the difference between working
and failing, so `src/llm/provider.py` stays ours.

**The checkpoint holds decisions, not resources.** The repository
snapshot and the open HTTP client are passed in at invoke time and never
written to state. They are derived from the head SHA, so a resumed run
rebuilds them; writing them to a checkpoint would be storing a temporary
directory that no longer exists.


## `src/judge/investigate.py`

<a id="src-judge-investigate"></a>

**Verification that can go and look.**

`src/judge/verify.py` asks a second model whether each finding holds up,
but shows it only what the generator already saw. When that is not
enough it answers `unverifiable`, and the finding is kept at a
discount:

    UNVERIFIABLE_PENALTY = 0.7

That constant is an admission. It exists because the judge wanted to
check something and had no way to check it. This module gives the judge
the toolbox, so it can open the file it was wondering about.

Two choices keep it affordable, since verification carries most of the
per-review bill.

**Selective.** Only a high-severity finding, or one the generator was
unsure about, earns a tool loop. A medium-severity finding the generator
was confident about goes to the cheap single call.

**Batched per file, not per candidate.** Candidates cluster in the files
the pull request touched and their evidence overlaps almost entirely.
One loop per file opens that file once instead of once per candidate.

Verification is also the best-shaped place in the system for a loop: it
arrives with a specific claim to test, so it converges in a few turns.
An agent told to "find bugs" has no natural stopping point.


## `src/recovery.py`

<a id="src-recovery"></a>

**Picking up reviews whose process died part way through.**

Two facts make this necessary. GitHub never retries a delivery, so nothing
arrives to prompt a second attempt. And the uniqueness rule on repository,
pull request and commit refuses a second claim on the same commit, so even a
manual redelivery used to be turned away. A review interrupted by a crash or
a restart was stuck in "processing" for good.

So the service looks for them itself, when it starts.

Running on startup rather than on a timer is a deliberate limit. The common
cause of an interrupted review is the process stopping, and a process that
stops is a process that starts again. What this does not catch is a review
abandoned by a process that is still running, which would need a scheduler.
