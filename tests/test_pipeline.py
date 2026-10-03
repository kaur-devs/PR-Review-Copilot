import pytest
from sqlalchemy import select

from src.db.models import Review
from src.db.queries import claim_review, ensure_repo
from src.db.session import get_session_factory
from src.github import auth
from src.pipeline import process_pull_request
from src.webhooks.events import PullRequestEvent
from tests.fakes import api_file, fake_github

PATCH = "@@ -17,4 +17,4 @@\n def get_user(user_id):\n-    return _USERS[user_id]\n+    return _USERS.get(user_id)\n \n"

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


async def make_review() -> int:
    async with get_session_factory()() as session:
        repo_id = await ensure_repo(session, EVENT)
        return await claim_review(session, repo_id, EVENT)


async def review_row(review_id: int):
    async with get_session_factory()() as session:
        result = await session.execute(
            select(Review.status, Review.attempts, Review.error_code).where(
                Review.id == review_id
            )
        )
        return result.one()


async def run_pipeline(pages):
    review_id = await make_review()
    client, calls = fake_github(pages)
    await process_pull_request(EVENT, review_id, github_client=client)
    return review_id, calls


async def test_a_review_moves_out_of_received_and_counts_the_attempt():
    review_id, _ = await run_pipeline([[api_file("models.py", patch=PATCH)]])

    status, attempts, error_code = await review_row(review_id)
    assert status == "processing"
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


async def test_reviewable_files_keep_the_review_in_progress():
    review_id, _ = await run_pipeline([[
        api_file("models.py", patch=PATCH),
        api_file("poetry.lock", patch=PATCH),
    ]])

    status, _, _ = await review_row(review_id)
    assert status == "processing"


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


async def test_a_review_already_being_worked_on_is_left_alone():
    review_id, _ = await run_pipeline([[api_file("models.py", patch=PATCH)]])

    client, calls = fake_github([[api_file("models.py", patch=PATCH)]])
    await process_pull_request(EVENT, review_id, github_client=client)

    assert calls == []
    status, attempts, _ = await review_row(review_id)
    assert status == "processing"
    assert attempts == 2
