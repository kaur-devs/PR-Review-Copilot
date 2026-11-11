"""Two reviewers, looking for different things, at the same time.

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
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from src.agents.answers import read_json_object
from src.agents.loop import AgentRun, run_agent
from src.agents.prompts import ROBUST, SPECIALISTS, WRONG, system_prompt
from src.agents.tools import SCHEMAS, Toolbox
from src.common import trace
from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import Finding
from src.review.generate import read_finding
from src.review.prompts import build_user_prompt
from src.review.provider import InvalidResponse, LLMProvider

logger = logging.getLogger(__name__)

# A specialist that returns more than this is pattern-matching, not
# reviewing. The cap exists so one noisy agent cannot flood the
# verification stage, which is the expensive one.
MAX_CANDIDATES_PER_SPECIALIST = 4


@dataclass
class SpecialistOutcome:
    findings: list[Finding] = field(default_factory=list)
    runs: dict[str, AgentRun] = field(default_factory=dict)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    # Which agent found what. Kept because "the security prompt found
    # nothing on any pull request" is a finding about the prompt, and it
    # is invisible once the lists are joined.
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
    model: LLMProvider,
    toolbox: Toolbox,
    user: str,
    diffs_by_file: dict[str, FileDiff],
    fallback_evidence: tuple[str, ...],
) -> tuple[str, AgentRun, list[Finding], list[tuple[str, str]]]:
    run = await run_agent(
        model, toolbox, system=system_prompt(which), user=user, tools=SCHEMAS
    )

    if not run.answered:
        logger.warning("the %s specialist produced nothing: %s", which, run)
        return which, run, [], []

    try:
        entries = _parse(run.answer)
    except (InvalidResponse, json.JSONDecodeError) as error:
        logger.warning("the %s specialist did not return usable JSON: %s", which, error)
        return which, run, [], [(which, f"unusable answer: {error}")]

    kept: list[Finding] = []
    dropped: list[tuple[str, str]] = []

    for entry in entries:
        if len(kept) >= MAX_CANDIDATES_PER_SPECIALIST:
            dropped.append((which, "over the per-specialist cap"))
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
        kept.append(finding)

    return which, run, kept, dropped


async def review(
    model: LLMProvider,
    toolbox: Toolbox,
    diffs: list[FileDiff],
    context: ContextBundle,
    *,
    only: tuple[str, ...] = SPECIALISTS,
) -> SpecialistOutcome:
    """Run the specialists triage asked for and gather what they found."""
    outcome = SpecialistOutcome()
    wanted = [which for which in only if which in SPECIALISTS]

    if not wanted or not diffs:
        return outcome

    user = build_user_prompt(diffs, context)
    diffs_by_file = {diff.filename: diff for diff in diffs}
    fallback_evidence = tuple(item.file for item in context.items)

    results = await asyncio.gather(
        *(
            _one_specialist(
                which, model, toolbox, user, diffs_by_file, fallback_evidence
            )
            for which in wanted
        ),
        return_exceptions=True,
    )

    for which, result in zip(wanted, results):
        # One specialist failing must not throw away the other's work.
        if isinstance(result, BaseException):
            logger.exception(
                "the %s specialist raised", which, exc_info=result
            )
            outcome.dropped.append((which, f"raised: {result}"))
            continue

        name, run, findings, dropped = result
        outcome.runs[name] = run
        outcome.found_by[name] = findings
        outcome.findings.extend(findings)
        outcome.dropped.extend(dropped)

    outcome.findings.sort(key=lambda finding: finding.sort_key)

    trace.section(
        "SPECIALISTS",
        f"{len(outcome.findings)} candidates · {outcome.turns} turns · "
        f"{outcome.tool_calls} tool calls",
    )
    for name, run in outcome.runs.items():
        trace.item(f"{name:<7} {run}")

    return outcome


__all__ = [
    "MAX_CANDIDATES_PER_SPECIALIST",
    "ROBUST",
    "SPECIALISTS",
    "SpecialistOutcome",
    "WRONG",
    "review",
]
