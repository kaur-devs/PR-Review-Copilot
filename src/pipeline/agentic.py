"""Run one pull request through the agentic graph.

Design note: docs/30_Design_Notes.md#src-agentic_run
"""

from __future__ import annotations

import logging
import os
import time

from src.agents.tools import Toolbox
from src.config_file import RepoConfig
from src.config_file import load as load_repo_config
from src.common import trace
from src.context.gather import gather_context
from src.db.queries import ensure_repo
from src.db.session import get_session_factory
from src.index.retrieve import candidates_for, ensure_indexed
from src.context.snapshot import (
    SnapshotTooLarge,
    SnapshotUnavailable,
    open_snapshot,
)
from src.filter.repeat import SaidBefore
from src.pipeline.graph import compile_graph
from src.pipeline.prepare import collect_diffs
from src.judge.verify import judge_model_name
from src.llm.provider import LLMNotConfigured, LLMProvider, quota
from src.pipeline.single_pass import (
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


async def _index_candidates(event, snapshot, diffs) -> list:
    """What else in the repository still says a name this change removed.

    Its own session, and its own try: the name index is an improvement to
    retrieval, not a thing a review should fail over. A repository being
    indexed for the first time pays for it once; afterwards only the
    changed files are re-read.
    """
    if not diffs:
        return []

    try:
        async with get_session_factory()() as session:
            repo_id = await ensure_repo(session, event)
            await ensure_indexed(
                session, repo_id, snapshot, event.head_sha,
                [diff.filename for diff in diffs],
            )
            found = await candidates_for(session, repo_id, snapshot, diffs)
    except Exception:
        logger.exception("the name index failed, reviewing without it")
        return []

    if found:
        trace.section(
            "NAME INDEX",
            f"{len(found)} file(s) still mention a name this change removed",
        )
        for item in found:
            trace.item(
                f"{item.file}:{item.reference_lines[0]}  "
                f"{item.changed_symbol} ({item.confidence})"
            )
    return found


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

    trace.banner(
        f"AGENTIC  ·  {event.repo_full_name}#{event.pr_number}  "
        f"·  {event.head_sha[:7]}"
    )

    if not await collect_diffs(event, outcome, github_client=github_client):
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
        async with open_snapshot(
            event.repo_full_name, event.head_sha, event.installation_id,
            client=github_client,
        ) as snapshot:
            outcome.context = await gather_context(
                event.repo_full_name, event.head_sha, event.installation_id,
                outcome.diffs, snapshot=snapshot,
            )
            outcome.context.items.extend(
                await _index_candidates(event, snapshot, outcome.diffs)
            )
            settings = load_repo_config(snapshot.root)
            if settings != RepoConfig():
                trace.section(
                    "REPOSITORY SETTINGS",
                    f"confidence {settings.minimum_confidence}, "
                    f"severities {', '.join(sorted(settings.severities))}, "
                    f"at most {settings.max_comments} comments"
                    + (
                        f", ignoring {', '.join(sorted(settings.ignore_categories))}"
                        if settings.ignore_categories else ""
                    ),
                )
            outcome.repo_config = settings

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
                        "skipped_files": outcome.skipped_files,
                    },
                    {
                        "configurable": {
                            "thread_id": thread_id(event),
                            "model": model,
                            "judge": judge,
                            "toolbox": Toolbox(snapshot),
                            "repo_config": settings,
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

    # Last, so a pull request with no findings makes no network calls.
    outcome.suggestions = await _settle_suggestions(
        kept, set(), event, github_client
    )
    outcome.findings = outcome.suggestions.findings

    trace.section(
        "SUGGESTIONS",
        f"{outcome.suggestions.offered} offered, "
        f"{len(outcome.suggestions.refused)} refused",
    )
    # The single-pass pipeline has always printed these; this one lost them
    # when the two were split, so four refusals in review 20 said nothing
    # about why a comment arrived with no Apply button.
    for finding, reason in outcome.suggestions.refused:
        trace.item(f"refused  {finding.file}:{finding.line}  {reason}")
        trace.snippet(finding.suggestion or "")

    outcome.model_calls = quota.requests - calls_before
    outcome.prompt_tokens = quota.prompt_tokens
    outcome.completion_tokens = quota.completion_tokens
    outcome.seconds = time.monotonic() - started
    outcome.turns = final.get("turns", 0)
    return outcome


__all__ = ["agentic_enabled", "review_pull_request_with_agents", "thread_id"]
