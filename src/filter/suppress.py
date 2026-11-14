"""Falling silent on a category a repository keeps ignoring.

The product requirements have carried this as US-006 and "not built"
since the beginning, with the `outcomes` table and the
`ix_outcomes_repo_category` index already in place for exactly this
query. This is the query.

What counts as evidence needs care, because GitHub only tells us when a
thread is resolved. There is no "this comment was wrong" event, so a
rejection cannot be stored and an acceptance rate taken over stored rows
alone would always be 100%. The denominator is therefore every finding
this repository has had posted in that category, and the numerator is
the ones somebody resolved. A category that gets posted often and
resolved rarely is a category nobody is acting on.

It is deliberately slow to act. Nothing is suppressed until a repository
has posted a real number of findings in a category, because two ignored
comments is a quiet week, not a verdict.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Finding, Outcome, Review

logger = logging.getLogger(__name__)

# Below this many posted findings a category has told us nothing.
MINIMUM_POSTED = 10

# Resolve rate under this, with enough evidence, means stop saying it.
ACCEPTANCE_FLOOR = 0.2


async def acceptance_by_category(
    session: AsyncSession, repo_id: int
) -> dict[str, tuple[int, int]]:
    """For each category: how many were posted, how many were resolved."""
    posted = (
        select(Finding.category, func.count().label("n"))
        .join(Review, Review.id == Finding.review_id)
        .where(Review.repo_id == repo_id, Finding.posted.is_(True))
        .group_by(Finding.category)
    )
    accepted = (
        select(Outcome.category, func.count().label("n"))
        .where(Outcome.repo_id == repo_id)
        .group_by(Outcome.category)
    )

    resolved = {row.category: row.n for row in await session.execute(accepted)}
    return {
        row.category: (row.n, resolved.get(row.category, 0))
        for row in await session.execute(posted)
    }


async def suppressed_categories(
    session: AsyncSession, repo_id: int
) -> dict[str, str]:
    """Categories this repository has earned the right not to hear about.

    Returns the reason alongside each, because a reviewer that goes quiet
    without being able to say why is indistinguishable from a broken one.
    """
    quiet: dict[str, str] = {}

    for category, (posted, resolved) in (
        await acceptance_by_category(session, repo_id)
    ).items():
        if posted < MINIMUM_POSTED:
            continue
        rate = resolved / posted
        if rate < ACCEPTANCE_FLOOR:
            quiet[category] = (
                f"{resolved} of {posted} {category} findings were acted on "
                f"in this repository ({rate:.0%})"
            )

    if quiet:
        logger.info("suppressing %s in repo %s", ", ".join(quiet), repo_id)
    return quiet


def apply(findings, quiet: dict[str, str]):
    """Split findings into those still worth posting and those held.

    Held, not deleted: the finding is still stored, so turning the
    threshold back down recovers it without re-running the reviewer.
    """
    if not quiet:
        return list(findings), []

    keep, held = [], []
    for finding in findings:
        reason = quiet.get(finding.category)
        if reason is None:
            keep.append(finding)
        else:
            held.append((finding, reason))
    return keep, held
