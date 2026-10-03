"""Send a webhook to your local service without needing a tunnel.

Two modes.

Fake, for quick checks:

    .venv/bin/python scripts/send_webhook.py
    .venv/bin/python scripts/send_webhook.py --delivery same-id      (run twice)
    .venv/bin/python scripts/send_webhook.py --action labeled
    .venv/bin/python scripts/send_webhook.py --bad-signature

Real, which looks up the actual installation, repository and commit so the
pipeline fetches a genuine pull request:

    .venv/bin/python scripts/send_webhook.py --real --repo kaur-devs/codebase-chat --pr 3

Also accepts a payload saved from GitHub's Recent Deliveries page:

    .venv/bin/python scripts/send_webhook.py --file saved.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_REPO = "kaur-devs/codebase-chat"


def fake_payload(repo: str, action: str, sha: str, pr_number: int) -> dict:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "head": {"sha": sha.ljust(40, "0")},
            "base": {"sha": "base".ljust(40, "0")},
        },
        "repository": {"id": 987654321, "full_name": repo},
        "installation": {"id": 55555555},
    }


async def real_payload(repo: str, action: str, pr_number: int) -> dict:
    import httpx

    from src.github.auth import API_HEADERS, GITHUB_API, build_app_jwt
    from src.github.client import GitHubClient

    async with httpx.AsyncClient(timeout=15) as http:
        headers = {**API_HEADERS, "Authorization": f"Bearer {build_app_jwt()}"}
        response = await http.get(f"{GITHUB_API}/app/installations", headers=headers)
        response.raise_for_status()
        installations = response.json()

    if not installations:
        raise SystemExit("The App is not installed anywhere. Run scripts/check_auth.py")

    installation_id = installations[0]["id"]

    async with GitHubClient(installation_id) as github:
        repository = await github.get(f"/repos/{repo}")
        pull_request = await github.get(f"/repos/{repo}/pulls/{pr_number}")

    print(
        f"using installation {installation_id}, repo id {repository['id']}, "
        f"head {pull_request['head']['sha'][:7]}"
    )

    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "head": {"sha": pull_request["head"]["sha"]},
            "base": {"sha": pull_request["base"]["sha"]},
        },
        "repository": {"id": repository["id"], "full_name": repository["full_name"]},
        "installation": {"id": installation_id},
    }


def send(url: str, body: bytes, signature: str, event: str, delivery: str) -> int:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            print(f"{response.status}  {response.read().decode()}")
    except urllib.error.HTTPError as error:
        print(f"{error.code}  {error.read().decode()}")
    except urllib.error.URLError as error:
        print(f"Could not reach {url} — is uvicorn running?  ({error.reason})")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--action", default="opened")
    parser.add_argument("--sha", default="aaa111")
    parser.add_argument("--pr", type=int, default=1)
    parser.add_argument("--delivery", default=None)
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--file", default=None)
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/github")
    parser.add_argument("--bad-signature", action="store_true")
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set. Check your .env file.")
        return 1

    if args.file:
        body = Path(args.file).read_bytes()
    elif args.real:
        payload = asyncio.run(real_payload(args.repo, args.action, args.pr))
        body = json.dumps(payload).encode()
    else:
        body = json.dumps(fake_payload(args.repo, args.action, args.sha, args.pr)).encode()

    if args.bad_signature:
        signature = "sha256=" + "0" * 64
    else:
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    delivery = args.delivery or f"local-{os.urandom(4).hex()}"
    return send(args.url, body, signature, args.event, delivery)


if __name__ == "__main__":
    sys.exit(main())
