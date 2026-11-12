from __future__ import annotations

import logging

from src.common import trace
from src.db.queries import (
    previously_posted,
    record_posted_review,
    set_review_status,
    start_attempt,
    store_findings,
)
from src.db.session import get_session_factory
from src.filter.gate import GateResult, apply_gate
from src.filter.repeat import drop_repeats
from src.github.client import GitHubClient
from src.github.review import post_review
from src.pipeline.agentic import agentic_enabled, review_pull_request_with_agents
from src.pipeline.single_pass import SKIP_NO_MODEL, review_pull_request
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_SKIPPED = "skipped"
STATUS_NO_FINDINGS = "no_findings"
STATUS_ALL_REPEATS = "all_repeats"
STATUS_POSTING = "posting"
STATUS_POSTED = "posted"
STATUS_SUPERSEDED = "superseded"
STATUS_FAILED = "failed"

ERROR_UNEXPECTED = "PIPELINE_UNEXPECTED"
ERROR_NO_MODEL = "LLM_NOT_CONFIGURED"
ERROR_POST_FAILED = "COMMENT_POST_FAILED"


async def process_pull_request(
    event: PullRequestEvent, review_id: int, *, github_client=None, llm_client=None
) -> None:
    """Review one pull request and post what it found.

    The database is touched in short bursts, never held open across the
    review itself. A review spends most of its time waiting on a rate
    limit — ten minutes is ordinary on a free tier — and a connection left
    idle that long is closed by the provider underneath us. The first real
    run ended exactly there: three verified findings, a written summary,
    and `connection is closed` on the final UPDATE, so nothing was posted.

    A transaction is not a place to keep state while waiting for someone
    else's network.
    """
    async with get_session_factory()() as session:
        await start_attempt(session, review_id)

        claimed = await set_review_status(
            session, review_id, expected=STATUS_RECEIVED, new=STATUS_PROCESSING
        )

    if not claimed:
        logger.info("review %s is no longer waiting to be processed", review_id)
        return

    try:
        await _record_and_post(event, review_id, github_client, llm_client)
    except Exception:
        logger.exception("review %s failed", review_id)
        trace.footer(f"REVIEW {review_id} FAILED")
        await _mark_failed(review_id, ERROR_UNEXPECTED)


async def _status(review_id: int, *, expected: str, new: str, **extra) -> bool:
    """One status change on a session of its own."""
    async with get_session_factory()() as session:
        return await set_review_status(
            session, review_id, expected=expected, new=new, **extra
        )


async def _mark_failed(review_id: int, error_code: str) -> None:
    """Record a failure, whichever status the review died in.

    Its own session, because the one that raised may be the reason: a
    session holding a broken connection refuses every further statement
    with PendingRollbackError, which is how the first real failure lost
    both the review and the record of why.
    """
    for expected in (STATUS_PROCESSING, STATUS_POSTING):
        try:
            if await _status(
                review_id, expected=expected, new=STATUS_FAILED,
                error_code=error_code,
            ):
                return
        except Exception:
            logger.exception("review %s: could not record the failure", review_id)
            return


async def _record_and_post(
    event: PullRequestEvent, review_id: int, github_client, llm_client
) -> None:
    trace.banner(
        f"REVIEW {review_id}  ·  {event.repo_full_name}#{event.pr_number}  "
        f"·  {event.head_sha[:7]}"
    )

    if agentic_enabled():
        async with get_session_factory()() as session:
            already_said = await previously_posted(session, review_id)

        outcome = await review_pull_request_with_agents(
            event,
            github_client=github_client,
            llm_client=llm_client,
            already_said=already_said,
        )
    else:
        outcome = await review_pull_request(
            event, github_client=github_client, llm_client=llm_client
        )

    # Still ours? A newer commit arriving while we were reviewing retires
    # this review, and the status no longer being "processing" is how we
    # find out. Posting now would comment on a commit nobody is reading.
    still_ours = await _status(
        review_id, expected=STATUS_PROCESSING, new=STATUS_PROCESSING,
        context_status=outcome.context.status,
    )
    if not still_ours:
        trace.footer(f"REVIEW {review_id} SUPERSEDED  ·  a newer commit arrived")
        logger.info("review %s was superseded while it ran", review_id)
        return

    def done(message: str) -> None:
        trace.footer(
            f"REVIEW {review_id} {message}  ·  "
            f"{outcome.seconds:.1f}s  ·  {outcome.model_calls} model calls"
        )

    if outcome.skipped == SKIP_NO_MODEL:
        done("FAILED  ·  no model configured")
        await _status(
            review_id, expected=STATUS_PROCESSING, new=STATUS_FAILED,
            error_code=ERROR_NO_MODEL,
        )
        return

    if not outcome.ran:
        done(f"DONE  ·  {outcome.skipped}")
        await _status(
            review_id, expected=STATUS_PROCESSING, new=STATUS_SKIPPED
        )
        return

    async with get_session_factory()() as session:
        stored = await store_findings(session, review_id, outcome.findings)
    logger.info("review %s: %s findings stored", review_id, stored)

    # The graph runs the gates itself, in a different order, so that
    # nothing is verified that could never be posted. Re-running them
    # here would be harmless but would report the held-back count twice.
    gate = (
        GateResult(post=outcome.findings, hold=outcome.held)
        if agentic_enabled()
        else apply_gate(outcome.findings)
    )

    trace.section("GATE", f"{len(gate.post)} to post · {len(gate.hold)} held back")
    for finding, reason in gate.hold:
        trace.item(f"held  {finding.file}:{finding.line}  ({reason})")

    if not gate.post:
        done(f"DONE  ·  {stored} findings, none worth posting")
        await _status(
            review_id, expected=STATUS_PROCESSING, new=STATUS_NO_FINDINGS
        )
        return

    # Said after the gate, not before: a finding held back last time was never
    # seen by anyone, so saying it now is new rather than a repeat.
    async with get_session_factory()() as session:
        said_before = await previously_posted(session, review_id)
    repeated = drop_repeats(gate.post, said_before)

    trace.section(
        "REPEATS",
        f"{len(repeated.fresh)} new · {len(repeated.repeats)} already said",
    )
    for finding, earlier in repeated.repeats:
        trace.item(
            f"said before  {finding.file}:{finding.line}  "
            f"({finding.category} at line {earlier.line} in an earlier review)"
        )

    if not repeated.fresh:
        done(f"DONE  ·  {len(repeated.repeats)} findings, all said already")
        await _status(
            review_id, expected=STATUS_PROCESSING, new=STATUS_ALL_REPEATS
        )
        return

    await _status(review_id, expected=STATUS_PROCESSING, new=STATUS_POSTING)

    try:
        async with GitHubClient(event.installation_id, client=github_client) as github:
            posted = await post_review(
                github, event.repo_full_name, event.pr_number, event.head_sha,
                repeated.fresh, held_back=len(gate.hold),
                summary=outcome.summary,
            )
    except Exception:
        logger.exception("review %s could not be posted", review_id)
        done("FAILED  ·  could not post comments")
        await _status(
            review_id, expected=STATUS_POSTING, new=STATUS_FAILED,
            error_code=ERROR_POST_FAILED,
        )
        return

    async with get_session_factory()() as session:
        matched = await record_posted_review(
            session, review_id, posted.github_review_id, posted.comments
        )

    await _status(review_id, expected=STATUS_POSTING, new=STATUS_POSTED)

    trace.section("POSTED", f"review {posted.github_review_id}")
    trace.item(posted.html_url or "(no url returned)")
    trace.item(f"{matched} of {len(gate.post)} comments matched back to findings")

    done(f"DONE  ·  {len(gate.post)} posted, {len(gate.hold)} held")
