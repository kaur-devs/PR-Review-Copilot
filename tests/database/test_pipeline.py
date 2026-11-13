import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.db.models import Review
from src.db.queries import claim_review, ensure_repo
from src.db.session import get_session_factory
from src.github import auth
from src.pipeline import process_pull_request
from src.webhooks.events import PullRequestEvent
from tests.fakes import api_file, fake_github, fake_model, make_patch

PATCH = make_patch(
    ["def get_user(user_id):", "    return _USERS[user_id]", ""],
    ["def get_user(user_id):", "    return _USERS.get(user_id)", ""],
    start=17,
)

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


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


async def make_review(event=EVENT) -> int:
    async with get_session_factory()() as session:
        repo_id = await ensure_repo(session, event)
        return await claim_review(session, repo_id, event)


async def review_row(review_id: int):
    async with get_session_factory()() as session:
        result = await session.execute(
            select(Review.status, Review.attempts, Review.error_code).where(
                Review.id == review_id
            )
        )
        return result.one()


async def run_pipeline(pages, *, findings=None, classifications=None, event=EVENT):
    review_id = await make_review(event)
    client, calls = fake_github(pages)
    model = fake_model(classifications=classifications, findings=findings)
    await process_pull_request(
        event, review_id, github_client=client, llm_client=model
    )
    return review_id, calls


async def test_a_review_with_nothing_to_say_ends_in_no_findings():
    review_id, _ = await run_pipeline([[api_file("models.py", patch=PATCH)]])

    status, attempts, error_code = await review_row(review_id)
    assert status == "no_findings"
    assert attempts == 1
    assert error_code is None


async def test_the_changed_files_are_fetched_from_the_right_place():
    _, calls = await run_pipeline([[api_file("models.py", patch=PATCH)]])

    data_calls = [c for c in calls if not c.url.path.endswith("/access_tokens")]
    assert data_calls[0].url.path == "/repos/kaur-devs/pr-review-sandbox/pulls/7/files"


async def test_a_pull_request_of_only_lockfiles_is_skipped():
    review_id, _ = await run_pipeline([[
        api_file("poetry.lock", patch=PATCH),
        api_file("node_modules/x.js", patch=PATCH),
    ]])

    status, _, _ = await review_row(review_id)
    assert status == "skipped"


async def test_a_pull_request_with_no_diffs_at_all_is_skipped():
    review_id, _ = await run_pipeline([[api_file("logo.png", patch=None)]])

    status, _, _ = await review_row(review_id)
    assert status == "skipped"


async def test_lockfiles_are_dropped_before_the_model_is_asked():
    review_id, _ = await run_pipeline([[
        api_file("models.py", patch=PATCH),
        api_file("poetry.lock", patch=PATCH),
    ]])

    status, _, _ = await review_row(review_id)
    assert status == "no_findings"


async def test_a_failure_marks_the_review_failed_with_a_code():
    import httpx

    def refuse(request):
        if request.url.path.endswith("/access_tokens"):
            from tests.fakes import token_response
            return token_response()
        return httpx.Response(500, json={"message": "server error"})

    review_id = await make_review()
    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    await process_pull_request(EVENT, review_id, github_client=client)

    status, attempts, error_code = await review_row(review_id)
    assert status == "failed"
    assert attempts == 1
    assert error_code == "PIPELINE_UNEXPECTED"


async def test_a_review_already_finished_is_left_alone():
    review_id, _ = await run_pipeline([[api_file("models.py", patch=PATCH)]])

    client, calls = fake_github([[api_file("models.py", patch=PATCH)]])
    await process_pull_request(EVENT, review_id, github_client=client)

    assert calls == []
    status, attempts, _ = await review_row(review_id)
    assert status == "no_findings"
    assert attempts == 2


async def test_findings_are_stored_against_the_review():
    from sqlalchemy import select

    from src.db.models import Finding

    review_id, _ = await run_pipeline(
        [[api_file("models.py", patch=PATCH)]],
        classifications={"models.py": "logic"},
        findings=[{
            "file": "models.py",
            "line": 18,
            "severity": "high",
            "category": "correctness",
            "message": "Callers assume a user is returned",
            "rationale": "service.py reads user.email straight after.",
            "confidence": 0.8,
        }],
    )

    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(Finding.file, Finding.line, Finding.severity, Finding.posted)
            .where(Finding.review_id == review_id)
        )).all()

    assert rows == [("models.py", 18, "high", True)]

    status, _, _ = await review_row(review_id)
    assert status == "posted"


async def test_a_finding_on_a_line_outside_the_diff_is_not_stored():
    review_id, _ = await run_pipeline(
        [[api_file("models.py", patch=PATCH)]],
        findings=[{
            "file": "models.py", "line": 9000, "severity": "high",
            "category": "correctness", "message": "x", "rationale": "y",
            "confidence": 0.9,
        }],
    )

    status, _, _ = await review_row(review_id)
    assert status == "no_findings"


async def test_without_a_model_the_review_fails_with_a_clear_code(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    review_id = await make_review()
    client, _ = fake_github([[api_file("models.py", patch=PATCH)]])
    await process_pull_request(EVENT, review_id, github_client=client)

    status, _, error_code = await review_row(review_id)
    assert status == "failed"
    assert error_code == "LLM_NOT_CONFIGURED"


async def test_the_judge_can_drop_a_finding_before_it_is_posted():
    from sqlalchemy import func, select

    from src.db.models import Finding

    review_id = await make_review()
    client, _ = fake_github([[api_file("models.py", patch=PATCH)]])
    model = fake_model(
        findings=[{
            "file": "models.py", "line": 18, "severity": "high",
            "category": "correctness", "message": "x", "rationale": "y",
            "confidence": 0.9,
        }],
        verdicts=[{"index": 0, "supported": False, "reason": "already handled"}],
    )

    await process_pull_request(EVENT, review_id, github_client=client, llm_client=model)

    async with get_session_factory()() as session:
        stored = await session.scalar(
            select(func.count()).select_from(Finding).where(Finding.review_id == review_id)
        )

    assert stored == 0
    status, _, _ = await review_row(review_id)
    assert status == "no_findings"


async def test_a_finding_the_judge_supports_is_posted():
    review_id = await make_review()
    client, _ = fake_github([[api_file("models.py", patch=PATCH)]])
    model = fake_model(
        findings=[{
            "file": "models.py", "line": 18, "severity": "high",
            "category": "correctness", "message": "x", "rationale": "y",
            "confidence": 0.9,
        }],
        verdicts=[{"index": 0, "supported": True, "reason": "confirmed"}],
    )

    await process_pull_request(EVENT, review_id, github_client=client, llm_client=model)

    status, _, _ = await review_row(review_id)
    assert status == "posted"


# --- Not saying the same thing twice ---

AFTER_A_PUSH = replace(EVENT, head_sha="c" * 40, delivery_id="d-2")

FILES = [[api_file("models.py", patch=PATCH)]]
LABELS = {"models.py": "logic"}


def model_finding(line=18, category="correctness", confidence=0.9, message=None):
    return {
        "file": "models.py",
        "line": line,
        "severity": "high",
        "category": category,
        "message": message or "Callers assume a user is returned",
        "rationale": "service.py reads user.email straight after.",
        "confidence": confidence,
    }


async def test_a_finding_already_posted_is_not_posted_again():
    await run_pipeline(FILES, classifications=LABELS, findings=[model_finding()])

    second_id, calls = await run_pipeline(
        FILES, classifications=LABELS, findings=[model_finding()], event=AFTER_A_PUSH
    )

    assert [c for c in calls if c.url.path.endswith("/reviews")] == []

    status, _, _ = await review_row(second_id)
    assert status == "all_repeats"


async def test_wording_changing_between_runs_does_not_defeat_it():
    await run_pipeline(FILES, classifications=LABELS, findings=[model_finding()])

    second_id, calls = await run_pipeline(
        FILES,
        classifications=LABELS,
        findings=[model_finding(message="A totally different sentence this time")],
        event=AFTER_A_PUSH,
    )

    assert [c for c in calls if c.url.path.endswith("/reviews")] == []
    assert (await review_row(second_id))[0] == "all_repeats"


async def test_a_genuinely_new_finding_still_gets_posted():
    await run_pipeline(FILES, classifications=LABELS, findings=[model_finding()])

    second_id, calls = await run_pipeline(
        FILES,
        classifications=LABELS,
        findings=[model_finding(), model_finding(category="performance")],
        event=AFTER_A_PUSH,
    )

    reviews = [c for c in calls if c.url.path.endswith("/reviews")]
    assert len(reviews) == 1

    body = json.loads(reviews[0].content)
    assert len(body["comments"]) == 1

    assert (await review_row(second_id))[0] == "posted"


async def test_something_held_back_by_the_gate_is_not_a_repeat():
    # Never posted, so nobody saw it: saying it now is new, not a repeat.
    first_id, _ = await run_pipeline(
        FILES, classifications=LABELS, findings=[model_finding(confidence=0.3)]
    )
    assert (await review_row(first_id))[0] == "no_findings"

    second_id, calls = await run_pipeline(
        FILES,
        classifications=LABELS,
        findings=[model_finding(confidence=0.9)],
        event=AFTER_A_PUSH,
    )

    assert len([c for c in calls if c.url.path.endswith("/reviews")]) == 1
    assert (await review_row(second_id))[0] == "posted"


# ------------------------------------------- no connection held across a review

async def test_no_database_session_is_open_while_the_review_runs():
    """A review spends most of its time waiting on a rate limit — ten
    minutes is ordinary on a free tier — and a connection left idle that
    long is closed underneath us. The first real run died exactly there,
    after finding and verifying three real problems.

    So the test is structural: while the review is running, nothing may be
    holding a session.
    """
    import src.pipeline.record as pipeline

    open_sessions = 0
    peak_while_reviewing = None

    real_factory = get_session_factory()

    class Counted:
        def __init__(self):
            self.session = real_factory()

        async def __aenter__(self):
            nonlocal open_sessions
            open_sessions += 1
            return await self.session.__aenter__()

        async def __aexit__(self, *exc_info):
            nonlocal open_sessions
            open_sessions -= 1
            return await self.session.__aexit__(*exc_info)

    async def watched_review(event, **kwargs):
        nonlocal peak_while_reviewing
        peak_while_reviewing = open_sessions
        raise RuntimeError("stop here; the session count is what matters")

    review_id = await make_review()
    client, _ = fake_github([[api_file("models.py", patch=PATCH)]])

    original = pipeline.get_session_factory
    pipeline.get_session_factory = lambda: Counted
    pipeline.review_pull_request, real_review = watched_review, pipeline.review_pull_request
    try:
        await process_pull_request(EVENT, review_id, github_client=client)
    finally:
        pipeline.get_session_factory = original
        pipeline.review_pull_request = real_review

    assert peak_while_reviewing == 0
    assert open_sessions == 0


async def test_a_failure_is_recorded_even_when_the_session_is_broken():
    """The session that raised may be the reason: once its connection has
    gone, every further statement on it fails with PendingRollbackError,
    which is how the first real failure lost both the review and the
    record of why it failed."""
    import src.pipeline.record as pipeline

    async def poisoned(event, **kwargs):
        raise RuntimeError("connection is closed")

    review_id = await make_review()
    original = pipeline.review_pull_request
    pipeline.review_pull_request = poisoned
    try:
        await process_pull_request(EVENT, review_id, github_client=None)
    finally:
        pipeline.review_pull_request = original

    status, _, error_code = await review_row(review_id)
    assert status == "failed"
    assert error_code == "PIPELINE_UNEXPECTED"


async def test_a_review_superseded_while_it_ran_posts_nothing():
    """A newer commit arrived mid-review. The comments it was about to
    post describe a commit nobody is reading now."""
    from src.db.queries import supersede_earlier_reviews

    review_id = await make_review()

    async def newer_commit_arrives(event, **kwargs):
        # Ours, not GitHub's: ensure_repo maps one to the other.
        async with get_session_factory()() as session:
            our_repo_id = await session.scalar(
                select(Review.repo_id).where(Review.id == review_id)
            )
            await supersede_earlier_reviews(
                session, our_repo_id, event.pr_number, keep=-1
            )
        return await real(event, **kwargs)

    import src.pipeline.record as pipeline
    real = pipeline.review_pull_request
    pipeline.review_pull_request = newer_commit_arrives

    client, calls = fake_github([[api_file("models.py", patch=PATCH)]])
    try:
        await process_pull_request(EVENT, review_id, github_client=client)
    finally:
        pipeline.review_pull_request = real

    status, _, _ = await review_row(review_id)
    assert status == "superseded"
    assert [c for c in calls if c.url.path.endswith("/reviews")] == []
