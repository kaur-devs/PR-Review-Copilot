"""Two reviewers, looking for different things, at the same time.

Design note: docs/30_Design_Notes.md#src-agents-specialists
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace

from src.agents.answers import read_json_object
from src.agents.loop import MAX_TURNS, AgentRun, run_agent
from src.agents.prompts import (
    PROMPT_VERSION,
    ROBUST,
    SPECIALISTS,
    WRONG,
    system_prompt,
)
from src.agents.tools import SCHEMAS, Toolbox
from src.common import trace
from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import Finding
from src.review.generate import read_finding
from src.review.prompts import build_user_prompt
from src.llm.provider import InvalidResponse, LLMProvider

logger = logging.getLogger(__name__)

# Each specialist reads one changed file at a time. Eighteen files in one
# conversation produced three tool calls and five candidates across 887
# lines: the model triaged to whatever it noticed first and stopped. A
# narrow scope is what makes it dig, which is the whole argument for the
# loop in the first place.
MAX_CANDIDATES_PER_FILE = 3

# So one noisy agent cannot flood verification, the expensive stage.
MAX_CANDIDATES_TOTAL = 15

# Tokens per minute is the binding limit, so the fan-out is bounded too.
MAX_CONCURRENT_AGENTS = 4

# Two findings this close together in one file are the same problem seen
# twice. Review 23 posted three comments for one unguarded fromisoformat,
# two of them on adjacent lines of the same function. Exact-line matching
# cannot see that; a developer reading the pull request can.
SAME_PROBLEM_LINES = 2

# A small change does not need a deep exploration budget. ChatPanel.jsx
# changed 23 lines, spent eight turns and 23,768 tokens, and found
# nothing; meanwhile main.py changed 5 and the CORS bug was visible in
# them. So the shallow pass is fewer turns, never fewer files.
SMALL_CHANGE_LINES = 25
TURNS_FOR_A_SMALL_CHANGE = 2


def _turn_budget(diff: FileDiff) -> int:
    """How far an agent may range, by how much actually changed."""
    changed = len(diff.commentable_lines)
    if changed <= SMALL_CHANGE_LINES:
        return TURNS_FOR_A_SMALL_CHANGE
    return MAX_TURNS

# Kept for callers and tests that still speak in per-specialist terms.
MAX_CANDIDATES_PER_SPECIALIST = MAX_CANDIDATES_TOTAL


@dataclass
class SpecialistOutcome:
    findings: list[Finding] = field(default_factory=list)
    runs: dict[str, AgentRun] = field(default_factory=dict)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    # "This prompt never finds anything" is invisible once lists are joined.
    found_by: dict[str, list[Finding]] = field(default_factory=dict)

    @property
    def turns(self) -> int:
        return sum(run.turn_count for run in self.runs.values())

    @property
    def tool_calls(self) -> int:
        return sum(run.tool_calls for run in self.runs.values())

    @property
    def tokens(self) -> int:
        return sum(run.tokens for run in self.runs.values())


def _parse(answer: str) -> list[dict]:
    """Read the findings list out of whatever the agent replied with."""
    raw = read_json_object(answer).get("findings")
    if not isinstance(raw, list):
        raise InvalidResponse("no findings list in the answer")
    return raw


def _evidence_of(entry: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """What the agent says it looked at, or what we gave it."""
    if isinstance(entry, dict):
        claimed = entry.get("evidence_files")
        if isinstance(claimed, list):
            named = tuple(str(item) for item in claimed if isinstance(item, str))
            if named:
                return named
    return fallback


async def _one_specialist(
    which: str,
    scope: str,
    model: LLMProvider,
    toolbox: Toolbox,
    user: str,
    diffs_by_file: dict[str, FileDiff],
    fallback_evidence: tuple[str, ...],
    max_turns: int = MAX_TURNS,
) -> tuple[str, str, AgentRun, list[Finding], list[tuple[str, str]]]:
    """One specialist on one changed file.

    `diffs_by_file` is the whole pull request, not just `scope`: an agent
    reading a renamed key in one file must be able to report the finding
    against the file that still reads the old name. Narrowing the anchor
    to the scope would reject exactly the cross-file findings this fan-out
    exists to collect.
    """
    run = await run_agent(
        model,
        toolbox,
        system=system_prompt(which),
        user=user,
        tools=SCHEMAS,
        max_turns=max_turns,
    )

    if not run.answered:
        logger.info("%s on %s produced nothing: %s", which, scope, run)
        return which, scope, run, [], []

    try:
        entries = _parse(run.answer)
    except (InvalidResponse, json.JSONDecodeError) as error:
        logger.warning("%s on %s did not return usable JSON: %s", which, scope, error)
        return which, scope, run, [], [(which, f"unusable answer on {scope}: {error}")]

    kept: list[Finding] = []
    dropped: list[tuple[str, str]] = []

    for entry in entries:
        if len(kept) >= MAX_CANDIDATES_PER_FILE:
            dropped.append((which, f"over the per-file cap on {scope}"))
            break
        finding, reason = read_finding(
            entry,
            diffs_by_file,
            _evidence_of(entry, fallback_evidence),
            allow_reanchor=True,
        )
        if finding is None:
            dropped.append((which, reason))
            if isinstance(entry, dict):
                trace.rejected(entry, reason)
            continue
        kept.append(
            replace(
                finding,
                model=model.model,
                found_by=which,
                prompt_version=PROMPT_VERSION,
            )
        )

    return which, scope, run, kept, dropped


def _dedup(findings: list[Finding]) -> tuple[list[Finding], int]:
    """One comment per line, however many agents noticed the problem.

    Two specialists on one file, and two agents on either end of a
    cross-file break, legitimately arrive at the same finding. The most
    confident wording wins; `sort_key` puts it first.

    Keyed on the line alone, not the line and the category. Review 22 put
    two comments on ScheduleBadge.tsx:24 for one unguarded dereference,
    one filed as error_handling and one as correctness — the same bug
    wearing two labels. Two genuinely separate problems on one line is
    possible and would now be merged; one problem reported twice is what
    actually happens, and a developer reading the same complaint twice is
    the noise this project exists to avoid.
    """
    best: dict[tuple[str, int], Finding] = {}
    duplicates = 0

    for finding in sorted(findings, key=lambda item: item.sort_key):
        near = next(
            (
                kept
                for kept in best.values()
                if kept.file == finding.file
                and abs(kept.line - finding.line) <= SAME_PROBLEM_LINES
            ),
            None,
        )
        if near is not None:
            duplicates += 1
            continue
        best[(finding.file, finding.line)] = finding

    return list(best.values()), duplicates


async def review(
    model: LLMProvider,
    toolbox: Toolbox,
    diffs: list[FileDiff],
    context: ContextBundle,
    *,
    only: tuple[str, ...] = SPECIALISTS,
) -> SpecialistOutcome:
    """Run the specialists triage asked for, one changed file at a time."""
    outcome = SpecialistOutcome()
    wanted = [which for which in only if which in SPECIALISTS]

    if not wanted or not diffs:
        return outcome

    diffs_by_file = {diff.filename: diff for diff in diffs}
    fallback_evidence = tuple(item.file for item in context.items)
    limit = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    async def one(which: str, diff: FileDiff):
        async with limit:
            return await _one_specialist(
                which,
                diff.filename,
                model,
                toolbox,
                build_user_prompt([diff], context),
                diffs_by_file,
                fallback_evidence,
                _turn_budget(diff),
            )

    jobs = [(which, diff) for which in wanted for diff in diffs]
    results = await asyncio.gather(
        *(one(which, diff) for which, diff in jobs), return_exceptions=True
    )

    for (which, diff), result in zip(jobs, results):
        # One file failing must not throw away every other file's work.
        if isinstance(result, BaseException):
            logger.exception(
                "%s raised on %s", which, diff.filename, exc_info=result
            )
            outcome.dropped.append((which, f"raised on {diff.filename}: {result}"))
            continue

        name, scope, run, findings, dropped = result
        outcome.runs[f"{name}:{scope}"] = run
        outcome.found_by.setdefault(name, []).extend(findings)
        outcome.findings.extend(findings)
        outcome.dropped.extend(dropped)

    outcome.findings, duplicates = _dedup(outcome.findings)

    if len(outcome.findings) > MAX_CANDIDATES_TOTAL:
        # Already severity-first, so what goes is the least serious.
        for extra in outcome.findings[MAX_CANDIDATES_TOTAL:]:
            outcome.dropped.append(
                ("all", f"over the {MAX_CANDIDATES_TOTAL}-candidate cap: {extra.file}")
            )
        outcome.findings = outcome.findings[:MAX_CANDIDATES_TOTAL]

    _trace(outcome, wanted, len(diffs), duplicates)
    return outcome


def _trace(
    outcome: SpecialistOutcome, wanted: list[str], files: int, duplicates: int
) -> None:
    """One line per specialist, not one per agent: 36 runs is not a report."""
    trace.section(
        "SPECIALISTS",
        f"{len(outcome.findings)} candidates · {len(outcome.runs)} agents over "
        f"{files} file(s) · {outcome.turns} turns · {outcome.tool_calls} tool "
        f"calls" + (f" · {duplicates} duplicate(s) merged" if duplicates else ""),
    )

    for which in wanted:
        runs = {
            scope.split(":", 1)[1]: run
            for scope, run in outcome.runs.items()
            if scope.startswith(f"{which}:")
        }
        found = len(outcome.found_by.get(which, []))
        trace.item(
            f"{which:<7} {found} found · {sum(r.turn_count for r in runs.values())} "
            f"turns · {sum(r.tool_calls for r in runs.values())} tool calls · "
            f"{sum(r.tokens for r in runs.values())} tokens"
        )
        # Only the files worth a second look: something found, or a stop
        # that was not the agent deciding it had finished.
        for scope, run in sorted(runs.items()):
            interesting = [
                finding
                for finding in outcome.found_by.get(which, [])
                if finding.file == scope
            ]
            if interesting or run.stopped_because not in (None, "answered"):
                trace.item(
                    f"  {scope}  {len(interesting)} found · {run.stopped_because}"
                )


__all__ = [
    "MAX_CANDIDATES_PER_SPECIALIST",
    "ROBUST",
    "SPECIALISTS",
    "SpecialistOutcome",
    "WRONG",
    "review",
]
