"""Fetching a pull request and deciding whether it is worth reviewing.

Both pipelines start here and used to start here separately, with the same
twenty lines written twice. The agentic one had quietly lost the diff trace
and the TRUNCATED BY GITHUB warning along the way, which is what a second
copy does rather than what it is for.
"""

from __future__ import annotations

import time

from src.common import trace
from src.diff.files import ChangedFileSet, fetch_changed_files
from src.diff.parse import parse_changed_files
from src.diff.skip import worth_reviewing
from src.github.client import GitHubClient
from src.webhooks.events import PullRequestEvent

SKIP_NOTHING_TO_REVIEW = "nothing worth reviewing"


async def collect_diffs(event: PullRequestEvent, outcome, *, github_client) -> bool:
    """Fill in the diffs and skipped files. False if there is nothing to review.

    `outcome` is filled in rather than returned so both pipelines keep
    owning their own ReviewOutcome, including the timing they started.
    """
    async with GitHubClient(event.installation_id, client=github_client) as github:
        file_set = await fetch_changed_files(
            github, event.repo_full_name, event.pr_number
        )

    keep, skipped = worth_reviewing(file_set.files)
    outcome.skipped_files = skipped
    outcome.diffs = [
        diff for diff in parse_changed_files(ChangedFileSet(files=keep)) if diff.parsed
    ]

    trace.section(
        "CHANGED FILES",
        f"{len(file_set)} changed · {len(keep)} reviewable · {len(skipped)} skipped"
        + (" · TRUNCATED BY GITHUB" if file_set.truncated else ""),
    )
    for changed in keep:
        trace.item(f"{changed.filename}   +{changed.additions} -{changed.deletions}")
    for filename, reason in skipped.items():
        trace.item(f"{filename}   skipped: {reason}")

    if outcome.diffs:
        trace.section(
            "DIFF",
            f"{sum(len(d.added_line_numbers) for d in outcome.diffs)} changed lines",
        )
        for diff in outcome.diffs:
            trace.item(diff.filename)
            trace.diff(diff)
        return True

    outcome.skipped = SKIP_NOTHING_TO_REVIEW
    return False


def finish(outcome, started: float, calls_before: int, quota) -> None:
    """The bookkeeping every review ends with, however it got there."""
    outcome.model_calls = quota.requests - calls_before
    outcome.seconds = time.monotonic() - started
