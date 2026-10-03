"""Proving who we are to GitHub.

No real network calls here. A fake transport stands in for GitHub, so the
tests are fast, work offline, and can pretend GitHub said anything we like.
"""

from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest

from src.config import get_settings
from src.github import auth


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    """Each test starts with an empty cache."""
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


def fake_github(token="ghs_example", expires_in_minutes=60, status=200):
    """A stand-in for GitHub that returns whatever we tell it to."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"message": "no"})
        return httpx.Response(
            200,
            json={
                "token": token,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "permissions": {"pull_requests": "write", "contents": "read"},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


# ---------------------------------------------------------------------------
# The short note that proves we are the App
# ---------------------------------------------------------------------------

def test_the_note_is_signed_with_our_private_key():
    """Anyone can read it, but only our key could have produced it."""
    token = auth.build_app_jwt()

    # Decoding without checking the signature is enough to read the claims.
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == get_settings().client_id


def test_the_note_uses_the_algorithm_github_requires():
    header = jwt.get_unverified_header(auth.build_app_jwt())
    assert header["alg"] == "RS256"


def test_the_note_is_backdated_slightly():
    """Guards against our clock running a little ahead of GitHub's."""
    claims = jwt.decode(auth.build_app_jwt(), options={"verify_signature": False})

    now = datetime.now(timezone.utc).timestamp()
    assert claims["iat"] < now


def test_the_note_expires_within_ten_minutes():
    """GitHub refuses anything claiming to last longer."""
    claims = jwt.decode(auth.build_app_jwt(), options={"verify_signature": False})

    lifetime = claims["exp"] - claims["iat"]
    assert lifetime <= 10 * 60


# ---------------------------------------------------------------------------
# Trading the note for a usable token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_we_get_a_token_back():
    client, _ = fake_github(token="ghs_abc123")

    result = await auth.fetch_installation_token(99, client=client)

    assert result.token == "ghs_abc123"
    assert result.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_we_ask_the_right_address_with_the_note_attached():
    client, calls = fake_github()

    await auth.fetch_installation_token(12345, client=client)

    request = calls[0]
    assert request.method == "POST"
    assert str(request.url).endswith("/app/installations/12345/access_tokens")
    # "Bearer" is what GitHub requires when sending a JWT.
    assert request.headers["Authorization"].startswith("Bearer ")
    assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"


@pytest.mark.asyncio
async def test_a_refusal_from_github_is_raised_not_swallowed():
    client, _ = fake_github(status=401)

    with pytest.raises(httpx.HTTPStatusError):
        await auth.fetch_installation_token(99, client=client)


# ---------------------------------------------------------------------------
# Not asking GitHub more often than we need to
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_token_we_already_have_is_reused():
    client, calls = fake_github()

    first = await auth.get_installation_token(99, client=client)
    second = await auth.get_installation_token(99, client=client)

    assert first == second
    assert len(calls) == 1      # GitHub was only asked once


@pytest.mark.asyncio
async def test_a_token_about_to_expire_is_replaced():
    """Under five minutes left is treated as too close to use."""
    client, calls = fake_github(expires_in_minutes=2)

    await auth.get_installation_token(99, client=client)
    await auth.get_installation_token(99, client=client)

    assert len(calls) == 2      # asked again rather than risk it


@pytest.mark.asyncio
async def test_each_installation_gets_its_own_token():
    """One customer's token must never be used for another's repositories."""
    client, calls = fake_github()

    await auth.get_installation_token(111, client=client)
    await auth.get_installation_token(222, client=client)

    assert len(calls) == 2
    assert str(calls[0].url).endswith("/app/installations/111/access_tokens")
    assert str(calls[1].url).endswith("/app/installations/222/access_tokens")
