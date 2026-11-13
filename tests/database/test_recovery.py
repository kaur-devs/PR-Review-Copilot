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
from src import recovery
from src.recovery import (
    ERROR_INTERRUPTED,
    MAX_RESUMED_PER_START,
    recover_interrupted_reviews,
    start_recovery,
)
from src.webhooks.events import PullRequestEvent
from tests.fakes import api_file, fake_github, fake_model, make_patch

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


# --- What has not changed ---

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


# --- Taking over an abandoned review ---

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

    # The first resets updated_at, so the second finds nothing to take.
    assert one == first
    assert two is None


# --- Finding what got stuck, and rebuilding enough to start it again ---

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


# --- Giving up on one that is out of attempts ---

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


# --- Picking them up on startup ---

PATCH = make_patch(
    ["def get_user(user_id):", "    return _USERS[user_id]", ""],
    ["def get_user(user_id):", "    return _USERS.get(user_id)", ""],
    start=17,
)

FILES = [[api_file("models.py", patch=PATCH)]]


async def abandon(review_id, *, status="processing", older_by=None):
    """Leave a review stuck, as a crash would."""
    if status != "received":
        await set_status(review_id, expected="received", new=status)
    await age(review_id, older_by or STALE_AFTER * 2)


async def recover(*, findings=None):
    client, calls = fake_github(FILES)
    model = fake_model(classifications={"models.py": "logic"}, findings=findings)
    report = await recover_interrupted_reviews(
        github_client=client, llm_client=model
    )
    return report, calls


async def test_nothing_stuck_means_nothing_happens():
    report, calls = await recover()

    assert report.resumed == []
    assert report.abandoned == []
    assert calls == []


async def test_a_live_review_is_left_alone():
    review_id, _ = await claim()
    await set_status(review_id, expected="received", new="processing")

    report, calls = await recover()

    assert report.resumed == []
    assert (await row(review_id))[0] == "processing"


async def test_an_interrupted_review_is_resumed_and_finishes():
    review_id, _ = await claim()
    await abandon(review_id)

    report, _ = await recover(findings=[{
        "file": "models.py", "line": 18, "severity": "high",
        "category": "correctness", "message": "callers assume a user",
        "rationale": "service.py reads user.email straight after.",
        "confidence": 0.9,
    }])

    assert report.resumed == [review_id]
    assert (await row(review_id))[0] == "posted"


async def test_resuming_counts_as_another_attempt():
    review_id, _ = await claim()
    await abandon(review_id)
    assert (await row(review_id))[1] == 0

    await recover()

    assert (await row(review_id))[1] == 1


async def test_a_review_out_of_attempts_is_given_up_on_not_resumed():
    review_id, _ = await claim()
    for _ in range(MAX_ATTEMPTS):
        async with get_session_factory()() as session:
            await start_attempt(session, review_id)
    await abandon(review_id)

    report, calls = await recover()

    assert report.abandoned == [review_id]
    assert report.resumed == []
    assert await row(review_id) == ("failed", MAX_ATTEMPTS, ERROR_INTERRUPTED)
    assert calls == []


async def test_only_a_few_are_resumed_per_start():
    stuck_ids = []
    for n in range(MAX_RESUMED_PER_START + 2):
        review_id, _ = await claim(
            replace(EVENT, head_sha=f"{n:040d}", delivery_id=f"d-{n}")
        )
        await abandon(review_id)
        stuck_ids.append(review_id)

    report, _ = await recover()

    assert len(report.resumed) == MAX_RESUMED_PER_START
    assert len(report.left_for_next_time) == 2


async def test_what_is_left_over_is_found_on_the_next_start():
    for n in range(MAX_RESUMED_PER_START + 1):
        review_id, _ = await claim(
            replace(EVENT, head_sha=f"{n:040d}", delivery_id=f"d-{n}")
        )
        await abandon(review_id)

    first, _ = await recover()
    leftover = first.left_for_next_time[0]

    # It is still stuck, so a second start picks it up.
    await age(leftover, STALE_AFTER * 2)
    second, _ = await recover()

    assert second.resumed == [leftover]


async def test_one_review_failing_does_not_stop_the_others(monkeypatch):
    bad, _ = await claim()
    await abandon(bad)
    good, _ = await claim(replace(EVENT, head_sha="c" * 40, delivery_id="d-2"))
    await abandon(good, older_by=STALE_AFTER)

    real = recovery.process_pull_request
    calls = []

    async def explode_once(event, review_id, **kwargs):
        calls.append(review_id)
        if review_id == bad:
            raise RuntimeError("the network fell over")
        return await real(event, review_id, **kwargs)

    monkeypatch.setattr(recovery, "process_pull_request", explode_once)

    report, _ = await recover()

    assert calls == [bad, good]
    assert report.resumed == [good]


async def test_recovery_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("RECOVERY_ON_STARTUP", "0")

    assert start_recovery() is None


async def test_the_app_starts_recovery_when_it_is_switched_on(monkeypatch):
    monkeypatch.setenv("RECOVERY_ON_STARTUP", "1")
    review_id, _ = await claim()
    await abandon(review_id)

    task = start_recovery()
    assert task is not None
    report = await task

    assert report.resumed == [review_id]
