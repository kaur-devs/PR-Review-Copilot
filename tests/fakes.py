from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx


def token_response() -> httpx.Response:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return httpx.Response(
        200,
        json={
            "token": "ghs_test",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        },
    )


def fake_github(pages: list[list[dict]]) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)

        if request.url.path.endswith("/access_tokens"):
            return token_response()

        page_number = int(request.url.params.get("page", 1))
        body = pages[page_number - 1]

        headers = {}
        if page_number < len(pages):
            next_url = str(request.url.copy_set_param("page", page_number + 1))
            headers["Link"] = f'<{next_url}>; rel="next"'

        return httpx.Response(200, json=body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


def api_file(
    filename: str,
    *,
    status: str = "modified",
    additions: int = 1,
    deletions: int = 0,
    patch: str | None = "@@ -1,1 +1,1 @@\n-old\n+new",
    previous_filename: str | None = None,
) -> dict:
    data = {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "changes": additions + deletions,
        "sha": "0" * 40,
    }
    if patch is not None:
        data["patch"] = patch
    if previous_filename is not None:
        data["previous_filename"] = previous_filename
    return data
