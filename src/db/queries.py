"""Reading and writing rows.

Design note: docs/30_Design_Notes.md#src-db-queries
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Finding, Installation, Repo, Review, WebhookEvent
from src.filter.repeat import SaidBefore
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)


# A review that has sat in one of these for longer than the timeout was
# abandoned: the process handling it died. Nothing else will ever move it on,
# because GitHub does not retry and the uniqueness rule refuses a second claim
# on the same commit.
UNFINISHED_STATUSES = ("received", "processing", "posting")

# A whole review takes tens of seconds. Fifteen minutes is long enough that a
# slow one is never mistaken for a dead one.
STALE_AFTER = timedelta(minutes=15)

# The attempts column exists to stop us retrying forever. A review that has
# already been started this many times is not going to work on the next try.
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class StuckReview:
    """A review left unfinished, with enough to start it again."""

    review_id: int
    attempts: int
    event: PullRequestEvent

    @property
    def can_retry(self) -> bool:
        return self.attempts < MAX_ATTEMPTS


async def record_delivery(
    session: AsyncSession,
    *,
    delivery_id: str,
    event: str,
    action: str | None,
    outcome: str,
    pull_request: PullRequestEvent | None = None,
) -> int | None:
    """Write down that GitHub sent us this message.

    Returns the new row's id, or None if we had already seen this message.
    None normally means someone pressed Redeliver in the GitHub interface.
    """
    statement = (
        insert(WebhookEvent)
        .values(
            delivery_id=delivery_id,
            event=event,
            action=action,
            outcome=outcome,
            # Only pull request events have these.
            github_repo_id=pull_request.repo_id if pull_request else None,
            pr_number=pull_request.pr_number if pull_request else None,
            head_sha=pull_request.head_sha if pull_request else None,
        )
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        # Returns nothing when the insert was refused, which is how the caller
        # learns it lost the race.
        .returning(WebhookEvent.id)
    )
    result = await session.execute(statement)
    await session.commit()
    return result.scalar_one_or_none()


async def update_delivery(
    session: AsyncSession, delivery_row_id: int, **fields: object
) -> None:
    """Change fields on a message we already recorded.

    Used for two things: linking it to the review it started, and correcting
    the outcome once we know more.
    """
    await session.execute(
        update(WebhookEvent).where(WebhookEvent.id == delivery_row_id).values(**fields)
    )
    await session.commit()


async def ensure_repo(session: AsyncSession, pull_request: PullRequestEvent) -> int:
    """Make sure we have rows for this installation and repository.

    Returns our own id for the repository.

    Repositories are matched on GitHub's number rather than the name, because
    a repository can be renamed. If the name has changed since we last saw it,
    we update our copy.
    """
    await session.execute(
        insert(Installation)
        .values(
            github_installation_id=pull_request.installation_id,
            account_login=pull_request.repo_full_name.split("/")[0],
        )
        .on_conflict_do_nothing(index_elements=["github_installation_id"])
    )
    installation_id = await session.scalar(
        select(Installation.id).where(
            Installation.github_installation_id == pull_request.installation_id
        )
    )

    # The repository. If we already know about it, refresh the name in case
    # it was renamed.
    await session.execute(
        insert(Repo)
        .values(
            installation_id=installation_id,
            github_repo_id=pull_request.repo_id,
            full_name=pull_request.repo_full_name,
        )
        .on_conflict_do_update(
            index_elements=["github_repo_id"],
            set_={
                "full_name": pull_request.repo_full_name,
                "installation_id": installation_id,
            },
        )
    )
    await session.commit()

    return await session.scalar(
        select(Repo.id).where(Repo.github_repo_id == pull_request.repo_id)
    )


async def claim_review(
    session: AsyncSession, repo_id: int, pull_request: PullRequestEvent
) -> int | None:
    """Claim this commit as ours to review.

    Returns the new review's id, or None if this commit is already being
    reviewed.

    "Claim" is the right word: whoever writes the row first owns the work.
    The unique rule on (repository, pull request number, commit) is what makes
    that safe. Two different messages about the same commit both arrive here,
    and only one gets a row back.

    A common case: someone reopens a pull request without pushing anything.
    That is a new message about code we have already reviewed, so it is
    correctly refused.
    """
    statement = (
        insert(Review)
        .values(
            repo_id=repo_id,
            pr_number=pull_request.pr_number,
            head_sha=pull_request.head_sha,
            status="received",
        )
        .on_conflict_do_nothing(index_elements=["repo_id", "pr_number", "head_sha"])
        .returning(Review.id)
    )
    result = await session.execute(statement)
    review_id = result.scalar_one_or_none()
    await session.commit()

    if review_id is not None:
        return review_id

    # The insert lost, so someone already holds this commit. That is normally
    # correct and we stop here. But if the holder was abandoned part way
    # through, nothing else is ever going to finish it.
    return await reclaim_abandoned_review(session, repo_id, pull_request)


async def supersede_earlier_reviews(
    session: AsyncSession, repo_id: int, pr_number: int, keep: int
) -> list[int]:
    """Retire unfinished reviews of older commits on this pull request.

    Returns the ids retired.

    Pushing twice in a minute is ordinary, and without this each push gets
    its own full review. They all finish, they all post, and the comments
    point at commits nobody is looking at any more. Worse on a free tier:
    they share one token budget, so two reviews of the same pull request
    each run at half speed.

    Only work that has not posted anything is retired. A review that
    already commented cannot be taken back, so "posting" and everything
    after it is left alone.
    """
    result = await session.execute(
        update(Review)
        .where(
            Review.repo_id == repo_id,
            Review.pr_number == pr_number,
            Review.id != keep,
            Review.status.in_(("received", "processing")),
        )
        .values(status="superseded")
        .returning(Review.id)
    )
    retired = list(result.scalars())
    await session.commit()

    if retired:
        logger.info(
            "superseded review(s) %s: a newer commit arrived on #%s",
            ", ".join(str(review_id) for review_id in retired),
            pr_number,
        )
    return retired


async def reclaim_abandoned_review(
    session: AsyncSession, repo_id: int, pull_request: PullRequestEvent
) -> int | None:
    """Take over a review of this commit that was left unfinished.

    Returns its id if we took it, or None if there was nothing to take.

    Written as one conditional update for the same reason claim_review uses
    ON CONFLICT: reading the row and then deciding would let two deliveries
    both decide to take over. The database runs one statement at a time, so
    exactly one of them gets a row back.

    The cutoff is computed by the database rather than in Python, so our clock
    and the database's clock disagreeing cannot change the answer.

    Note that "failed" is not in the list. A failed review was a decision, not
    an abandonment, and quietly retrying it would hide whatever caused it.
    """
    result = await session.execute(
        update(Review)
        .where(
            Review.repo_id == repo_id,
            Review.pr_number == pull_request.pr_number,
            Review.head_sha == pull_request.head_sha,
            Review.status.in_(UNFINISHED_STATUSES),
            Review.updated_at < func.now() - STALE_AFTER,
            Review.attempts < MAX_ATTEMPTS,
        )
        .values(status="received", error_code=None)
        .returning(Review.id)
    )
    review_id = result.scalar_one_or_none()
    await session.commit()

    if review_id is not None:
        logger.info("took over review %s, abandoned part way through", review_id)

    return review_id


async def start_attempt(session: AsyncSession, review_id: int) -> None:
    await session.execute(
        update(Review)
        .where(Review.id == review_id)
        .values(attempts=Review.attempts + 1)
    )
    await session.commit()


async def set_review_status(
    session: AsyncSession,
    review_id: int,
    *,
    expected: str,
    new: str,
    **extra: object,
) -> bool:
    """Move a review to a new status, but only if it is still in the old one.

    Returns True if the change happened.

    The "expected" check matters because work can overlap. If a review has
    already been moved on by something else, we must not quietly overwrite
    that. Checking the old status in the same statement makes the whole thing
    happen at once, with no gap for another change to slip into.
    """
    result = await session.execute(
        update(Review)
        .where(Review.id == review_id, Review.status == expected)
        .values(status=new, **extra)
    )
    await session.commit()
    return result.rowcount == 1


async def store_findings(session: AsyncSession, review_id: int, findings) -> int:
    if not findings:
        return 0

    session.add_all([
        Finding(
            review_id=review_id,
            file=finding.file,
            line=finding.line,
            severity=finding.severity,
            category=finding.category,
            rationale=finding.rationale or finding.message,
            confidence=finding.confidence,
            posted=False,
        )
        for finding in findings
    ])
    await session.commit()
    return len(findings)


async def record_posted_review(
    session: AsyncSession,
    review_id: int,
    github_review_id: int,
    comments,
) -> int:
    """Link our findings to the comments GitHub created for them.

    Matched one at a time rather than by lookup, because two findings can sit
    on the same line and would otherwise collapse into one.

    The comment ids are what later tells us whether a developer resolved or
    dismissed each one, which is how the project measures whether it helps.
    """
    await session.execute(
        update(Review)
        .where(Review.id == review_id)
        .values(github_review_id=github_review_id)
    )

    unclaimed = list(comments)
    matched = 0

    findings = (
        await session.execute(select(Finding).where(Finding.review_id == review_id))
    ).scalars().all()

    for finding in findings:
        match = next(
            (
                comment
                for comment in unclaimed
                if comment.file == finding.file and comment.line == finding.line
            ),
            None,
        )
        if match is not None:
            unclaimed.remove(match)
            finding.posted = True
            finding.github_comment_id = match.comment_id
            matched += 1

    await session.commit()
    return matched


async def previously_posted(
    session: AsyncSession, review_id: int
) -> list[SaidBefore]:
    """What earlier reviews of this same pull request already posted.

    Found through this review rather than from the event, so the caller does
    not have to know the difference between GitHub's numeric repository id and
    our own. Only posted findings count: one we held back was never seen, so
    saying it now is not a repeat.
    """
    current = (
        await session.execute(
            select(Review.repo_id, Review.pr_number).where(Review.id == review_id)
        )
    ).first()

    if current is None:
        return []

    repo_id, pr_number = current

    rows = await session.execute(
        select(Finding.file, Finding.line, Finding.category)
        .join(Review, Finding.review_id == Review.id)
        .where(
            Review.repo_id == repo_id,
            Review.pr_number == pr_number,
            Review.id != review_id,
            Finding.posted.is_(True),
        )
    )

    return [SaidBefore(file=file, line=line, category=category)
            for file, line, category in rows]


async def stuck_reviews(session: AsyncSession) -> list[StuckReview]:
    """Every review left unfinished past the timeout, oldest first.

    The event is rebuilt from the database rather than kept anywhere, which is
    the whole reason the pipeline takes a small object instead of a webhook
    payload. Three tables are needed for it: the review knows the pull request
    and the commit, the repository knows its name and GitHub's id for it, and
    the installation knows the number we need to get a token.

    base_sha comes back empty because it is not stored. Nothing in the review
    path reads it; if something ever does, it needs a column first.
    """
    rows = await session.execute(
        select(
            Review.id,
            Review.attempts,
            Review.pr_number,
            Review.head_sha,
            Repo.github_repo_id,
            Repo.full_name,
            Installation.github_installation_id,
        )
        .join(Repo, Review.repo_id == Repo.id)
        .join(Installation, Repo.installation_id == Installation.id)
        .where(
            Review.status.in_(UNFINISHED_STATUSES),
            Review.updated_at < func.now() - STALE_AFTER,
        )
        .order_by(Review.updated_at)
    )

    return [
        StuckReview(
            review_id=review_id,
            attempts=attempts,
            event=PullRequestEvent(
                delivery_id=f"resumed-{review_id}",
                action="resumed",
                installation_id=github_installation_id,
                repo_id=github_repo_id,
                repo_full_name=full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha="",
            ),
        )
        for (
            review_id, attempts, pr_number, head_sha,
            github_repo_id, full_name, github_installation_id,
        ) in rows
    ]


async def abandon_review(
    session: AsyncSession, review_id: int, *, error_code: str
) -> bool:
    """Give up on a review that is out of attempts.

    set_review_status needs to know the status it is moving from, and a stuck
    review could be in any of three. This accepts all of them, so the row
    stops claiming to be in progress when nothing is working on it.
    """
    result = await session.execute(
        update(Review)
        .where(Review.id == review_id, Review.status.in_(UNFINISHED_STATUSES))
        .values(status="failed", error_code=error_code)
    )
    await session.commit()
    return result.rowcount == 1


async def reopen_review(session: AsyncSession, review_id: int) -> bool:
    """Put a stuck review back to waiting, so the pipeline will take it.

    The pipeline only starts a review that is in "received", which is correct:
    it stops two things working on the same review. A review abandoned in
    "processing" therefore has to be moved back before it can be resumed.
    """
    result = await session.execute(
        update(Review)
        .where(Review.id == review_id, Review.status.in_(UNFINISHED_STATUSES))
        .values(status="received", error_code=None)
    )
    await session.commit()
    return result.rowcount == 1
