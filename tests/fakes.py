from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta, timezone

import httpx

ARCHIVE_PREFIX = "owner-repo-abc1234"


def token_response() -> httpx.Response:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return httpx.Response(
        200,
        json={
            "token": "ghs_test",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        },
    )


def build_tarball(entries: dict[str, bytes] | None = None) -> bytes:
    entries = entries or {"app/models.py": b"def unrelated():\n    return 1\n"}
    buffer = io.BytesIO()

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        root = tarfile.TarInfo(ARCHIVE_PREFIX)
        root.type = tarfile.DIRTYPE
        tar.addfile(root)

        for path, content in entries.items():
            info = tarfile.TarInfo(f"{ARCHIVE_PREFIX}/{path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    return buffer.getvalue()


def fake_github(
    pages: list[list[dict]], *, archive: bytes | None = None
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    posted_comments: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)

        if request.url.path.endswith("/access_tokens"):
            return token_response()

        if "/tarball/" in request.url.path:
            return httpx.Response(200, content=archive or build_tarball())

        if request.url.path.endswith("/reviews") and request.method == "POST":
            import json as _json

            body = _json.loads(request.content)
            posted_comments.clear()
            posted_comments.extend(body.get("comments", []))
            return httpx.Response(
                200,
                json={
                    "id": 909090,
                    "html_url": "https://github.com/o/r/pull/1#pullrequestreview-909090",
                    "body": body.get("body", ""),
                },
            )

        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[
                {"id": 7000 + n, "path": c["path"], "line": c["line"]}
                for n, c in enumerate(posted_comments)
            ])

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


def fake_model(
    *, classifications: dict[str, str] | None = None, findings: list[dict] | None = None
) -> httpx.AsyncClient:
    """Answers both kinds of request the pipeline makes of a model."""
    import json

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]

        if "label code changes" in system:
            answer = {
                "files": [
                    {"file": path, "kind": kind, "reason": "test"}
                    for path, kind in (classifications or {}).items()
                ]
            }
        else:
            answer = {"findings": findings or []}

        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def make_patch(before: list[str], after: list[str], *, start: int = 1) -> str:
    """Build a valid unified diff.

    Hand-written hunk headers are easy to get wrong, and a wrong one makes the
    diff unparseable, which quietly turns a test green for the wrong reason.
    """
    import difflib

    body = []
    matcher = difflib.SequenceMatcher(None, before, after)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            body.extend(f" {line}" for line in before[i1:i2])
        else:
            body.extend(f"-{line}" for line in before[i1:i2])
            body.extend(f"+{line}" for line in after[j1:j2])

    header = f"@@ -{start},{len(before)} +{start},{len(after)} @@"
    return header + "\n" + "\n".join(body)
