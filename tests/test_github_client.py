"""Making requests as an installation, without touching the real GitHub."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.github import auth
from src.github.client import GitHubClient


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


def fake_github(pages: list[list[dict]] | None = None):
    """Stands in for GitHub.

    Answers the token request, then serves the pages given to it, adding the
    "there is more" link between them the way GitHub does.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    pages = pages or [[{"filename": "a.py"}]]
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)

        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                200,
                json={
                    "token": "ghs_test",
                    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                },
            )

        page_number = int(request.url.params.get("page", 1))
        body = pages[page_number - 1]

        headers = {}
        if page_number < len(pages):
            next_url = str(request.url.copy_set_param("page", page_number + 1))
            headers["Link"] = f'<{next_url}>; rel="next"'

        return httpx.Response(200, json=body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


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
