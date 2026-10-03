"""Making requests as an installation, without touching the real GitHub."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.github import auth
from src.github.client import GitHubClient
from tests.fakes import fake_github as _fake_github


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


def fake_github(pages=None):
    return _fake_github(pages or [[{"filename": "a.py"}]])


async def test_a_request_carries_an_installation_token():
    client, calls = fake_github()

    async with GitHubClient(42, client=client) as github:
        await github.get("/repos/owner/name/pulls/1")

    data_request = calls[-1]
    # "token" is the word GitHub expects for an installation token. "Bearer"
    # is only for the App's own note.
    assert data_request.headers["Authorization"] == "token ghs_test"


async def test_a_single_page_comes_back_whole():
    client, _ = fake_github(pages=[[{"filename": "a.py"}, {"filename": "b.py"}]])

    async with GitHubClient(42, client=client) as github:
        files = await github.get_all_pages("/repos/owner/name/pulls/1/files")

    assert [f["filename"] for f in files] == ["a.py", "b.py"]


async def test_every_page_is_followed():
    """A pull request touching hundreds of files must not be silently cut off."""
    client, _ = fake_github(
        pages=[
            [{"filename": "one.py"}],
            [{"filename": "two.py"}],
            [{"filename": "three.py"}],
        ]
    )

    async with GitHubClient(42, client=client) as github:
        files = await github.get_all_pages("/repos/owner/name/pulls/1/files")

    assert [f["filename"] for f in files] == ["one.py", "two.py", "three.py"]


async def test_the_token_is_fetched_once_for_many_requests():
    client, calls = fake_github(pages=[[{"a": 1}], [{"b": 2}]])

    async with GitHubClient(42, client=client) as github:
        await github.get_all_pages("/repos/owner/name/pulls/1/files")

    token_requests = [c for c in calls if c.url.path.endswith("/access_tokens")]
    assert len(token_requests) == 1


async def test_an_error_from_github_is_raised():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            return httpx.Response(
                200,
                json={"token": "t", "expires_at": expires.isoformat().replace("+00:00", "Z")},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async with GitHubClient(42, client=client) as github:
        with pytest.raises(httpx.HTTPStatusError):
            await github.get("/repos/owner/missing/pulls/1")
