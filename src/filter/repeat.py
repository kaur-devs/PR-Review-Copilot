"""Not saying the same thing twice on one pull request.

Design note: docs/30_Design_Notes.md#src-filter-repeat
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.review.findings import Finding

logger = logging.getLogger(__name__)

# Matches the window the evaluation harness uses when deciding whether a
# finding hit an expected bug, for the same reason: exact line matching is too
# strict to be useful.
LINE_WINDOW = 3


@dataclass(frozen=True)
class SaidBefore:
    """A finding posted on this pull request by an earlier review."""

    file: str
    line: int
    category: str


@dataclass
class RepeatOutcome:
    fresh: list[Finding] = field(default_factory=list)
    repeats: list[tuple[Finding, SaidBefore]] = field(default_factory=list)


def is_the_same(finding: Finding, earlier: SaidBefore) -> bool:
    return (
        finding.file == earlier.file
        and finding.category == earlier.category
        and abs(finding.line - earlier.line) <= LINE_WINDOW
    )


def drop_repeats(
    findings: list[Finding], said_before: list[SaidBefore]
) -> RepeatOutcome:
    """Split findings into ones worth posting and ones we have already made."""
    outcome = RepeatOutcome()

    if not said_before:
        outcome.fresh = list(findings)
        return outcome

    for finding in findings:
        earlier = next(
            (older for older in said_before if is_the_same(finding, older)), None
        )
        if earlier is None:
            outcome.fresh.append(finding)
        else:
            outcome.repeats.append((finding, earlier))

    logger.info(
        "repeats: %s to post, %s already said", len(outcome.fresh), len(outcome.repeats)
    )
    return outcome
