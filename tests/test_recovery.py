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
    abandon_review,
    claim_review,
    ensure_repo,
    set_review_status,
    start_attempt,
    stuck_reviews,
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


# ---------------------------------------------------------------------------
# Finding what got stuck, and rebuilding enough to start it again
# ---------------------------------------------------------------------------

async def stuck():
    async with get_session_factory()() as session:
        return await stuck_reviews(session)


async def test_nothing_is_stuck_to_begin_with():
    assert await stuck() == []


async def test_a_live_review_is_not_stuck():
    review_id, _ = await claim()
    await set_status(review_id, expected="received", new="processing")

    assert await stuck() == []


async def test_a_finished_review_is_never_stuck():
    review_id, _ = await claim()
    await set_status(review_id, expected="received", new="posted")
    await age(review_id, STALE_AFTER * 2)

    assert await stuck() == []


async def test_an_abandoned_review_is_found():
    review_id, _ = await claim()
    await set_status(review_id, expected="received", new="processing")
    await age(review_id, STALE_AFTER * 2)

    found = await stuck()

    assert [s.review_id for s in found] == [review_id]


async def test_the_event_is_rebuilt_from_the_database():
    review_id, _ = await claim()
    await age(review_id, STALE_AFTER * 2)

    event = (await stuck())[0].event

    # Everything the review path actually reads comes back correct.
    assert event.installation_id == EVENT.installation_id
    assert event.repo_id == EVENT.repo_id
    assert event.repo_full_name == EVENT.repo_full_name
    assert event.pr_number == EVENT.pr_number
    assert event.head_sha == EVENT.head_sha


async def test_the_rebuilt_event_says_where_it_came_from():
    review_id, _ = await claim()
    await age(review_id, STALE_AFTER * 2)

    event = (await stuck())[0].event

    assert event.delivery_id == f"resumed-{review_id}"
    assert event.action == "resumed"


async def test_attempts_decide_whether_it_is_worth_retrying():
    review_id, _ = await claim()
    await age(review_id, STALE_AFTER * 2)
    assert (await stuck())[0].can_retry

    for _ in range(MAX_ATTEMPTS):
        async with get_session_factory()() as session:
            await start_attempt(session, review_id)
    await age(review_id, STALE_AFTER * 2)

    assert not (await stuck())[0].can_retry


async def test_the_oldest_comes_first():
    first, repo_id = await claim()
    second, _ = await claim(replace(EVENT, head_sha="c" * 40, delivery_id="d-2"))

    await age(second, STALE_AFTER * 2)
    await age(first, STALE_AFTER * 4)

    assert [s.review_id for s in await stuck()] == [first, second]


# ---------------------------------------------------------------------------
# Giving up on one that is out of attempts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stuck_in", ["received", "processing", "posting"])
async def test_a_review_can_be_abandoned_from_any_unfinished_status(stuck_in):
    review_id, _ = await claim()
    if stuck_in != "received":
        await set_status(review_id, expected="received", new=stuck_in)

    async with get_session_factory()() as session:
        assert await abandon_review(session, review_id, error_code="INTERRUPTED")

    assert await row(review_id) == ("failed", 0, "INTERRUPTED")


async def test_a_finished_review_is_not_abandoned():
    review_id, _ = await claim()
    await set_status(review_id, expected="received", new="posted")

    async with get_session_factory()() as session:
        assert not await abandon_review(session, review_id, error_code="INTERRUPTED")

    assert (await row(review_id))[0] == "posted"


async def test_an_abandoned_review_stops_showing_up_as_stuck():
    review_id, _ = await claim()
    await set_status(review_id, expected="received", new="processing")
    await age(review_id, STALE_AFTER * 2)

    async with get_session_factory()() as session:
        await abandon_review(session, review_id, error_code="INTERRUPTED")

    assert await stuck() == []
