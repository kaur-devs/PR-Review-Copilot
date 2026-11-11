"""Run one pull request through the agentic graph.

The counterpart to `src/review_run.py`, which runs the single-pass
pipeline. Both produce a ReviewOutcome, touch neither the database nor
GitHub's comment API, and can be pointed at the same evaluation cases,
which is the only way to answer whether the agents are worth their cost.

Which one runs is a setting, not a code path chosen at build time:

    AGENTIC_PIPELINE=1

This module owns the resources the graph is not allowed to hold. The
snapshot is a temporary directory and the providers are open HTTP
clients; both are opened here, passed in at invoke time, and closed when
the review ends. A checkpoint that tried to store them would be storing
a path that no longer exists.
"""

from __future__ import annotations

import logging
import os
import time

from src.agents.tools import Toolbox
from src.common import trace
from src.context.gather import gather_context
from src.context.snapshot import (
    SnapshotTooLarge,
    SnapshotUnavailable,
    open_snapshot,
)
from src.diff.files import ChangedFileSet, fetch_changed_files
from src.diff.parse import parse_changed_files
from src.diff.skip import worth_reviewing
from src.filter.repeat import SaidBefore
from src.github.client import GitHubClient
from src.graph import compile_graph
from src.judge.verify import judge_model_name
from src.review.provider import LLMNotConfigured, LLMProvider, quota
from src.review_run import (
    SKIP_NO_MODEL,
    SKIP_NOTHING_TO_REVIEW,
    ReviewOutcome,
    _settle_suggestions,
)
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

_graph = None


def agentic_enabled() -> bool:
    return os.getenv("AGENTIC_PIPELINE", "0").lower() in ("1", "true", "yes", "on")


def graph():
    """Compiled once. The topology does not change between reviews."""
    global _graph
    if _graph is None:
        _graph = compile_graph()
    return _graph


def thread_id(event: PullRequestEvent) -> str:
    """The idempotency key the project already has, reused as a thread.

    A checkpoint belongs to one commit on one pull request, which is
    exactly the key the reviews table is unique on.
    """
    return f"{event.repo_id}:{event.pr_number}:{event.head_sha}"


async def review_pull_request_with_agents(
    event: PullRequestEvent,
    *,
    github_client=None,
    llm_client=None,
    already_said: list[SaidBefore] | None = None,
    checkpointer=None,
) -> ReviewOutcome:
    started = time.monotonic()
    calls_before = quota.requests
    outcome = ReviewOutcome()

    async with GitHubClient(event.installation_id, client=github_client) as github:
        file_set = await fetch_changed_files(
            github, event.repo_full_name, event.pr_number
        )

    keep, skipped = worth_reviewing(file_set.files)
    outcome.skipped_files = skipped
    outcome.diffs = [
        diff for diff in parse_changed_files(ChangedFileSet(files=keep)) if diff.parsed
    ]

    trace.banner(
        f"AGENTIC  ·  {event.repo_full_name}#{event.pr_number}  "
        f"·  {event.head_sha[:7]}"
    )
    trace.section(
        "CHANGED FILES",
        f"{len(file_set)} changed · {len(keep)} reviewable · {len(skipped)} skipped",
    )
    for filename, reason in skipped.items():
        trace.item(f"{filename}   skipped: {reason}")

    if not outcome.diffs:
        outcome.skipped = SKIP_NOTHING_TO_REVIEW
        outcome.seconds = time.monotonic() - started
        return outcome

    try:
        model = LLMProvider.from_env(client=llm_client)
        judge = LLMProvider.from_env(client=llm_client, model=judge_model_name())
    except LLMNotConfigured as error:
        logger.warning("cannot review: %s", error)
        outcome.skipped = SKIP_NO_MODEL
        outcome.seconds = time.monotonic() - started
        return outcome

    try:
        # One snapshot, held open for as long as the agents are looking.
        # gather_context used to open its own and throw it away.
        async with open_snapshot(
            event.repo_full_name, event.head_sha, event.installation_id,
            client=github_client,
        ) as snapshot:
            outcome.context = await gather_context(
                event.repo_full_name, event.head_sha, event.installation_id,
                outcome.diffs, snapshot=snapshot,
            )
            trace.section(
                "CONNECTED CODE",
                f"{len(outcome.context.files)} files · "
                f"{outcome.context.characters} chars · {outcome.context.status}",
            )

            async with model, judge:
                final = await graph().ainvoke(
                    {
                        "diffs": outcome.diffs,
                        "context": outcome.context,
                        "already_said": already_said or [],
                        "skipped": skipped,
                    },
                    {
                        "configurable": {
                            "thread_id": thread_id(event),
                            "model": model,
                            "judge": judge,
                            "toolbox": Toolbox(snapshot),
                        }
                    },
                )

    except (SnapshotUnavailable, SnapshotTooLarge) as error:
        logger.warning("no snapshot for %s: %s", event.repo_full_name, error)
        outcome.skipped = f"the repository could not be read: {error}"
        outcome.seconds = time.monotonic() - started
        return outcome

    outcome.classifications = final.get("classifications", {})
    kept = final.get("to_post", [])
    outcome.summary = final.get("summary", "")
    outcome.held = final.get("held", [])

    # Suggestions last: they read files from GitHub, so a pull request
    # with no surviving findings makes no network calls at all.
    outcome.suggestions = await _settle_suggestions(
        kept, set(), event, github_client
    )
    outcome.findings = outcome.suggestions.findings

    trace.section(
        "SUGGESTIONS",
        f"{outcome.suggestions.offered} offered, "
        f"{len(outcome.suggestions.refused)} refused",
    )

    outcome.model_calls = quota.requests - calls_before
    outcome.seconds = time.monotonic() - started
    outcome.turns = final.get("turns", 0)
    return outcome


__all__ = ["agentic_enabled", "review_pull_request_with_agents", "thread_id"]
