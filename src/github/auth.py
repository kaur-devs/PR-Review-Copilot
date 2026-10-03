"""Proving to GitHub that we are who we say we are.

This happens in two steps, and understanding why there are two is worth a
minute.

Step one. Our App has a private key, which only we hold. We use it to sign a
short note saying "I am App number 5039285", valid for a few minutes. That
note is called a JSON Web Token, or JWT. Anyone can read it, but only someone
with our private key could have produced it.

That note proves we are the App. It does not let us read anybody's code.

Step two. We hand the note to GitHub and ask for a token for one particular
installation. GitHub checks the note, checks that installation really exists,
and gives back a token that works for one hour and can only touch the
repositories that installation was given.

Why bother with two steps? Because the second token is narrow and short-lived.
If it leaks, it expires within the hour and only ever reached one account. Our
private key, which never leaves this machine, stays safe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from src.config import get_settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# GitHub asks for these on every request. The version header means our code
# keeps working when they change the API for everybody else.
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
}

# GitHub refuses a JWT that claims to last more than ten minutes. We ask for
# nine, leaving room for our clock being slightly fast.
JWT_LIFETIME_SECONDS = 9 * 60

# GitHub recommends backdating the start time by a minute, in case our clock
# is slightly ahead of theirs. Without this, a token can be rejected as
# "issued in the future".
JWT_BACKDATE_SECONDS = 60

# Installation tokens last an hour. We fetch a new one when under five minutes
# remain, so a request never starts with a token that expires mid-flight.
REFRESH_WHEN_UNDER = timedelta(minutes=5)


def build_app_jwt() -> str:
    """Make the short note that proves we are this App.

    Signed with RS256, which means "signed with our private key in a way
    anyone can verify using the matching public key GitHub already has".
    """
    settings = get_settings()
    now = int(time.time())

    claims = {
        "iat": now - JWT_BACKDATE_SECONDS,   # issued at
        "exp": now + JWT_LIFETIME_SECONDS,   # expires at
        "iss": settings.client_id,           # who issued it: us
    }

    return jwt.encode(claims, settings.read_private_key(), algorithm="RS256")


@dataclass(frozen=True)
class InstallationToken:
    """A token that can read one installation's repositories, for one hour."""

    token: str
    expires_at: datetime

    @property
    def is_still_usable(self) -> bool:
        """False once it is expired or close enough that we should replace it."""
        return datetime.now(timezone.utc) + REFRESH_WHEN_UNDER < self.expires_at


# Tokens we have already fetched, kept by installation id so we are not
# asking GitHub for a new one on every single request.
_token_cache: dict[int, InstallationToken] = {}


def clear_token_cache() -> None:
    """Forget every cached token. Used by tests."""
    _token_cache.clear()


async def fetch_installation_token(
    installation_id: int, *, client: httpx.AsyncClient | None = None
) -> InstallationToken:
    """Ask GitHub for a fresh token for one installation.

    The client argument exists so tests can pass a fake one. In normal use it
    is left out and we make our own.
    """
    url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
    headers = {**API_HEADERS, "Authorization": f"Bearer {build_app_jwt()}"}

    # If we were not given a client, make one and close it afterwards.
    we_made_the_client = client is None
    if we_made_the_client:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        response = await client.post(url, headers=headers)
        response.raise_for_status()
        body = response.json()
    finally:
        if we_made_the_client:
            await client.aclose()

    # GitHub sends the expiry as text like "2026-09-27T17:00:00Z". The Z means
    # UTC, which Python wants written as "+00:00".
    expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))

    logger.info(
        "got a token for installation %s, good until %s",
        installation_id,
        expires_at.isoformat(timespec="seconds"),
    )
    return InstallationToken(token=body["token"], expires_at=expires_at)


async def get_installation_token(
    installation_id: int, *, client: httpx.AsyncClient | None = None
) -> str:
    """The token to use for this installation, fetching one only if needed."""
    cached = _token_cache.get(installation_id)
    if cached is not None and cached.is_still_usable:
        return cached.token

    fresh = await fetch_installation_token(installation_id, client=client)
    _token_cache[installation_id] = fresh
    return fresh.token
