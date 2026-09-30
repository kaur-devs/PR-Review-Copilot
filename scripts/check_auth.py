"""Check that we can authenticate with GitHub, step by step.

Run this whenever something looks wrong with credentials. It shows exactly
which step fails, which is much faster than guessing.

    .venv/bin/python scripts/check_auth.py

No secrets are printed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.github.auth import (  # noqa: E402
    API_HEADERS,
    GITHUB_API,
    build_app_jwt,
    get_installation_token,
)


async def main() -> int:
    # Step 1: sign a note with our private key.
    try:
        note = build_app_jwt()
    except Exception as error:
        print(f"FAILED to sign the App note: {error}")
        print("Check GITHUB_APP_PRIVATE_KEY_PATH points at a valid key file.")
        return 1
    print(f"1. signed our App note ({len(note)} characters)")

    async with httpx.AsyncClient(timeout=15) as http:
        app_headers = {**API_HEADERS, "Authorization": f"Bearer {note}"}

        # Step 2: does GitHub accept it?
        response = await http.get(f"{GITHUB_API}/app", headers=app_headers)
        if response.status_code != 200:
            print(f"FAILED: GitHub did not accept the note ({response.status_code})")
            print("Usually means GITHUB_CLIENT_ID is wrong, or the key was revoked.")
            return 1
        app = response.json()
        print(f"2. GitHub recognises us: '{app['slug']}' owned by '{app['owner']['login']}'")

        # Step 3: who has installed us?
        response = await http.get(f"{GITHUB_API}/app/installations", headers=app_headers)
        response.raise_for_status()
        installations = response.json()
        print(f"3. installations: {len(installations)}")

        if not installations:
            print()
            print("Nobody has installed the App yet, so there is nothing to read.")
            print(f"Install it here: https://github.com/apps/{app['slug']}/installations/new")
            return 1

        for installation in installations:
            print(
                f"     id={installation['id']}  "
                f"account={installation['account']['login']}  "
                f"repositories={installation['repository_selection']}"
            )

        # Step 4: trade the note for a token we can actually use.
        installation_id = installations[0]["id"]
        token = await get_installation_token(installation_id, client=http)
        print(f"4. got an installation token (starts '{token[:7]}...')")

        # Step 5: does that token actually open anything?
        response = await http.get(
            f"{GITHUB_API}/installation/repositories",
            headers={**API_HEADERS, "Authorization": f"token {token}"},
        )
        response.raise_for_status()
        repositories = response.json()
        print(f"5. repositories we can read ({repositories['total_count']}):")
        for repository in repositories["repositories"]:
            print(f"     {repository['full_name']}")

    print()
    print("All good.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
