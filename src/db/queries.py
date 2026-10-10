"""Reading and writing rows.

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
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Finding, Installation, Repo, Review, WebhookEvent
from src.webhooks.events import PullRequestEvent


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
        # If a row with this delivery_id already exists, change nothing.
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        # Give us back the id, but only if we actually inserted something.
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
    # The installation. If we already know about it, leave it alone.
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
    await session.commit()
    return result.scalar_one_or_none()


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
    comment_ids: dict[tuple[str, int], int],
) -> int:
    """Link our findings to the comments GitHub created for them.

    The comment ids are what later tells us whether a developer resolved or
    dismissed each one, which is how the project measures whether it helps.
    """
    await session.execute(
        update(Review)
        .where(Review.id == review_id)
        .values(github_review_id=github_review_id)
    )

    matched = 0
    findings = (
        await session.execute(select(Finding).where(Finding.review_id == review_id))
    ).scalars().all()

    for finding in findings:
        comment_id = comment_ids.get((finding.file, finding.line))
        if comment_id is not None:
            finding.posted = True
            finding.github_comment_id = comment_id
            matched += 1

    await session.commit()
    return matched
