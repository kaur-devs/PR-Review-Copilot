"""Proving to GitHub that we are who we say we are.

Design note: docs/30_Design_Notes.md#src-github-auth
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

# The version header keeps us working when the API changes for everyone else.
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
}

# GitHub refuses a JWT over ten minutes; nine leaves room for clock drift.
JWT_LIFETIME_SECONDS = 9 * 60

# Backdated a minute, or a fast clock gets the token rejected as future-dated.
JWT_BACKDATE_SECONDS = 60

# Refreshed with five minutes left, so nothing starts on a token about to die.
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


# Per installation, so a review does not re-trade a token on every request.
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

    # The trailing Z means UTC, which Python wants written as "+00:00".
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
