"""Where the review work happens.

This takes a PullRequestEvent and nothing else. It never sees the web request,
which matters for two reasons: the code is easy to test, and the evaluation
harness can replay old pull requests without any webhook involved.

Right now it only logs what it would do. The real stages get added here in
order: fetch the diff, gather context, classify, generate, judge, filter, post.
"""

from __future__ import annotations

import logging

from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)


async def process_pull_request(event: PullRequestEvent) -> None:
    """Review one commit on one pull request."""
    logger.info(
        "would review %s#%s at commit %s (action=%s)",
        event.repo_full_name,
        event.pr_number,
        event.head_sha[:7],
        event.action,
    )
