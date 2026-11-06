"""Reviews that were abandoned part way through.

GitHub never retries a delivery, and the uniqueness rule on
(repository, pull request, commit) refuses a second claim on the same commit.
Together those mean a review whose process died is stuck forever unless
something deliberately takes it over.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from src.db.models import Review
from src.db.queries import (
    MAX_ATTEMPTS,
    STALE_AFTER,
    claim_review,
    ensure_repo,
    set_review_status,
    start_attempt,
)
from src.db.session import get_session_factory
from src.webhooks.events import PullRequestEvent

EVENT = PullRequestEvent(
    delivery_id="d-1",
    action="opened",
    installation_id=166408152,
    repo_id=987654321,
    repo_full_name="kaur-devs/pr-review-sandbox",
    pr_number=7,
    head_sha="a" * 40,
    base_sha="b" * 40,
)


async def claim(event=EVENT) -> tuple[int, int]:
    """A fresh claim, returning the review id and the local repo id."""
    async with get_session_factory()() as session:
        repo_id = await ensure_repo(session, event)
        review_id = await claim_review(session, repo_id, event)
        return review_id, repo_id


async def age(review_id: int, by: timedelta) -> None:
    """Backdate a review's last change, as though time had passed."""
    async with get_session_factory()() as session:
        await session.execute(
            update(Review)
            .where(Review.id == review_id)
            .values(updated_at=datetime.now(timezone.utc) - by)
        )
        await session.commit()


async def row(review_id: int):
    async with get_session_factory()() as session:
        return (await session.execute(
            select(Review.status, Review.attempts, Review.error_code)
            .where(Review.id == review_id)
        )).one()


async def set_status(review_id: int, *, expected: str, new: str, **extra):
    async with get_session_factory()() as session:
        return await set_review_status(
            session, review_id, expected=expected, new=new, **extra
        )


# ---------------------------------------------------------------------------
# What has not changed
# ---------------------------------------------------------------------------

async def test_a_second_claim_on_a_live_review_is_still_refused():
    first, repo_id = await claim()
    await set_status(first, expected="received", new="processing")

    async with get_session_factory()() as session:
        assert await claim_review(session, repo_id, EVENT) is None


async def test_a_second_claim_on_a_finished_review_is_still_refused():
    first, repo_id = await claim()
    await set_status(first, expected="received", new="posted")
    await age(first, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        assert await claim_review(session, repo_id, EVENT) is None


async def test_a_failed_review_is_not_quietly_retried():
    # A failure was a decision. Retrying it would hide whatever caused it.
    first, repo_id = await claim()
    await set_status(first, expected="received", new="failed", error_code="SOMETHING")
    await age(first, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        assert await claim_review(session, repo_id, EVENT) is None


# ---------------------------------------------------------------------------
# Taking over an abandoned review
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stuck_in", ["received", "processing", "posting"])
async def test_a_review_abandoned_in_any_unfinished_status_is_taken_over(stuck_in):
    first, repo_id = await claim()
    if stuck_in != "received":
        await set_status(first, expected="received", new=stuck_in)
    await age(first, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        again = await claim_review(session, repo_id, EVENT)

    assert again == first
    assert (await row(first))[0] == "received"


async def test_taking_over_clears_the_old_error_code():
    first, repo_id = await claim()
    await set_status(
        first, expected="received", new="processing", error_code="LEFTOVER"
    )
    await age(first, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        await claim_review(session, repo_id, EVENT)

    status, _, error_code = await row(first)
    assert status == "received"
    assert error_code is None


async def test_a_review_stuck_for_less_than_the_timeout_is_left_alone():
    first, repo_id = await claim()
    await set_status(first, expected="received", new="processing")
    await age(first, STALE_AFTER / 2)

    async with get_session_factory()() as session:
        assert await claim_review(session, repo_id, EVENT) is None

    assert (await row(first))[0] == "processing"


async def test_a_review_that_has_run_out_of_attempts_is_not_taken_over():
    first, repo_id = await claim()
    await set_status(first, expected="received", new="processing")
    for _ in range(MAX_ATTEMPTS):
        async with get_session_factory()() as session:
            await start_attempt(session, first)
    await age(first, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        assert await claim_review(session, repo_id, EVENT) is None


async def test_only_this_commit_is_taken_over():
    first, repo_id = await claim()
    await set_status(first, expected="received", new="processing")
    await age(first, STALE_AFTER * 2)

    # A different commit on the same pull request is its own review.
    other = replace(EVENT, head_sha="c" * 40, delivery_id="d-2")
    async with get_session_factory()() as session:
        second = await claim_review(session, repo_id, other)

    assert second != first
    assert (await row(first))[0] == "processing"


async def test_two_deliveries_arriving_together_cannot_both_take_over():
    first, repo_id = await claim()
    await set_status(first, expected="received", new="processing")
    await age(first, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        one = await claim_review(session, repo_id, EVENT)
        two = await claim_review(session, repo_id, EVENT)

    # The first takes it over, which resets updated_at, so the second finds
    # nothing abandoned to take.
    assert one == first
    assert two is None
