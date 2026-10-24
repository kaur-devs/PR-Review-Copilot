"""Not saying the same thing twice on one pull request.

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
