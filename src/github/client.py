"""Making requests to GitHub on behalf of one installation.

Every call needs a token, every token belongs to one installation, and tokens
expire. Rather than making each part of the codebase remember all that, this
wraps it up: you say which installation you are acting for, then make calls.

    async with GitHubClient(installation_id) as github:
        files = await github.get(f"/repos/{repo}/pulls/{number}/files")
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.github.auth import API_HEADERS, GITHUB_API, get_installation_token

logger = logging.getLogger(__name__)

# How long to wait before giving up on a request. GitHub is normally fast;
# waiting forever would block a review indefinitely.
TIMEOUT_SECONDS = 15.0


class GitHubClient:
    """Talks to GitHub as one installation."""

    def __init__(
        self, installation_id: int, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.installation_id = installation_id

        # Tests pass their own fake client. Normally we make our own.
        self._client = client
        self._we_made_the_client = client is None

    async def __aenter__(self) -> "GitHubClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._we_made_the_client and self._client is not None:
            await self._client.aclose()

    async def _headers(self) -> dict[str, str]:
        """Our standard headers, with a currently valid token attached."""
        token = await get_installation_token(
            self.installation_id, client=self._client
        )
        # "token" rather than "Bearer" here. GitHub uses Bearer for the App's
        # own note, and this word for an installation token.
        return {**API_HEADERS, "Authorization": f"token {token}"}

    async def get(self, path: str, **params: Any) -> Any:
        """Fetch something. Path is relative, e.g. "/repos/owner/name/pulls/1"."""
        response = await self._client.get(
            GITHUB_API + path, headers=await self._headers(), params=params or None
        )
        response.raise_for_status()
        return response.json()

    async def get_all_pages(self, path: str, *, per_page: int = 100) -> list[Any]:
        """Fetch a list that GitHub splits across several pages.

        GitHub returns long lists in chunks and tells you there is more by
        including a "next" link in the response headers. A pull request with
        more than a hundred changed files is exactly the kind of pull request
        we must not get wrong, so we follow those links until they run out.
        """
        results: list[Any] = []
        url = GITHUB_API + path
        params: dict[str, Any] | None = {"per_page": per_page}

        while url:
            response = await self._client.get(
                url, headers=await self._headers(), params=params
            )
            response.raise_for_status()
            results.extend(response.json())

            # The next link already carries its own page number, so the
            # parameters must not be sent again.
            url = response.links.get("next", {}).get("url", "")
            params = None

        return results

    async def post(self, path: str, json: Any) -> Any:
        """Send something, for example a review."""
        response = await self._client.post(
            GITHUB_API + path, headers=await self._headers(), json=json
        )
        response.raise_for_status()
        return response.json()
