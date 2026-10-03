"""Send a fake GitHub webhook to your local service.

Useful because you do not need a tunnel, an internet connection, or a real
pull request. It signs the message with the secret from your .env file, so
the service accepts it exactly as it would accept a real one.

Examples:

    # A new pull request
    .venv/bin/python scripts/send_webhook.py

    # The same message again, to see duplicate handling
    .venv/bin/python scripts/send_webhook.py --delivery same-id
    .venv/bin/python scripts/send_webhook.py --delivery same-id

    # Someone pushed more commits
    .venv/bin/python scripts/send_webhook.py --action synchronize --sha bbb222

    # Something we do not review
    .venv/bin/python scripts/send_webhook.py --action labeled

    # A real payload you copied from GitHub's Recent Deliveries page
    .venv/bin/python scripts/send_webhook.py --file saved_payload.json
"""

from __future__ import annotations

import argparse
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
load_dotenv(PROJECT_ROOT / ".env")


def build_payload(action: str, sha: str, pr_number: int) -> dict:
    """A message shaped like GitHub's, with the fields our service reads."""
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "head": {"sha": sha.ljust(40, "0")},
            "base": {"sha": "base".ljust(40, "0")},
        },
        "repository": {"id": 987654321, "full_name": "kaur-devs/pr-review-sandbox"},
        "installation": {"id": 55555555},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", default="opened", help="opened, synchronize, ...")
    parser.add_argument("--sha", default="aaa111", help="the commit being reviewed")
    parser.add_argument("--pr", type=int, default=1, help="pull request number")
    parser.add_argument("--delivery", default=None, help="reuse to test duplicates")
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("--file", default=None, help="a saved payload to send instead")
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/github")
    parser.add_argument("--bad-signature", action="store_true", help="test rejection")
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set. Check your .env file.")
        return 1

    if args.file:
        body = Path(args.file).read_bytes()
    else:
        body = json.dumps(build_payload(args.action, args.sha, args.pr)).encode()

    # Sign it the same way GitHub does.
    if args.bad_signature:
        signature = "sha256=" + "0" * 64
    else:
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    delivery = args.delivery or f"local-{os.urandom(4).hex()}"

    request = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
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
        print(f"Could not reach {args.url} — is uvicorn running?  ({error.reason})")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
