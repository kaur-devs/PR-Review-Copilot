from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Finding, Outcome, Repo, Review
from src.outcomes.events import OUTCOME_ACCEPTED, ThreadEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordResult:
    matched: int
    unknown: int

    @property
    def anything_matched(self) -> bool:
        return self.matched > 0


async def record_thread_outcome(
    session: AsyncSession, event: ThreadEvent
) -> RecordResult:
    """Note that a developer resolved or reopened one of our comments.

    A thread can hold several comments, most of them replies from people. Only
    the ones we recognise as our own findings are recorded.
    """
    local_repo_id = await session.scalar(
        select(Repo.id).where(Repo.github_repo_id == event.repo_id)
    )
    if local_repo_id is None:
        logger.info("we have no record of %s", event.repo_full_name)
        return RecordResult(matched=0, unknown=len(event.comment_ids))

    findings = (
        await session.execute(
            select(Finding)
            .join(Review, Review.id == Finding.review_id)
            .where(
                Finding.github_comment_id.in_(event.comment_ids),
                Review.repo_id == local_repo_id,
            )
        )
    ).scalars().all()

    unknown = len(event.comment_ids) - len(findings)

    if not findings:
        logger.info(
            "review thread on %s#%s is not one of ours (%s comments)",
            event.repo_full_name,
            event.pr_number,
            len(event.comment_ids),
        )
        return RecordResult(matched=0, unknown=unknown)

    for finding in findings:
        if event.resolved:
            await session.execute(
                insert(Outcome)
                .values(
                    finding_id=finding.id,
                    repo_id=local_repo_id,
                    category=finding.category,
                    outcome=OUTCOME_ACCEPTED,
                )
                .on_conflict_do_update(
                    index_elements=["finding_id"],
                    set_={"outcome": OUTCOME_ACCEPTED, "category": finding.category},
                )
            )
        else:
            await session.execute(
                delete(Outcome).where(Outcome.finding_id == finding.id)
            )

    await session.commit()

    logger.info(
        "%s of our findings on %s#%s were marked %s",
        len(findings),
        event.repo_full_name,
        event.pr_number,
        "resolved" if event.resolved else "unresolved",
    )
    return RecordResult(matched=len(findings), unknown=unknown)
