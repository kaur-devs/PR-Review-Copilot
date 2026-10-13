from __future__ import annotations

import logging

from src.common import trace
from src.db.queries import (
    record_posted_review,
    set_review_status,
    start_attempt,
    store_findings,
)
from src.db.session import get_session_factory
from src.filter.gate import apply_gate
from src.github.client import GitHubClient
from src.github.review import post_review
from src.review_run import SKIP_NO_MODEL, review_pull_request
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_SKIPPED = "skipped"
STATUS_NO_FINDINGS = "no_findings"
STATUS_POSTING = "posting"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"

ERROR_UNEXPECTED = "PIPELINE_UNEXPECTED"
ERROR_NO_MODEL = "LLM_NOT_CONFIGURED"
ERROR_POST_FAILED = "COMMENT_POST_FAILED"


async def process_pull_request(
    event: PullRequestEvent, review_id: int, *, github_client=None, llm_client=None
) -> None:
    async with get_session_factory()() as session:
        await start_attempt(session, review_id)

        claimed = await set_review_status(
            session, review_id, expected=STATUS_RECEIVED, new=STATUS_PROCESSING
        )
        if not claimed:
            logger.info("review %s is no longer waiting to be processed", review_id)
            return

        try:
            await _record_and_post(session, event, review_id, github_client, llm_client)
        except Exception:
            logger.exception("review %s failed", review_id)
            trace.footer(f"REVIEW {review_id} FAILED")
            await set_review_status(
                session, review_id, expected=STATUS_PROCESSING, new=STATUS_FAILED,
                error_code=ERROR_UNEXPECTED,
            )


async def _record_and_post(
    session, event: PullRequestEvent, review_id: int, github_client, llm_client
) -> None:
    trace.banner(
        f"REVIEW {review_id}  ·  {event.repo_full_name}#{event.pr_number}  "
        f"·  {event.head_sha[:7]}"
    )

    outcome = await review_pull_request(
        event, github_client=github_client, llm_client=llm_client
    )

    await set_review_status(
        session, review_id, expected=STATUS_PROCESSING, new=STATUS_PROCESSING,
        context_status=outcome.context.status,
    )

    def done(message: str) -> None:
        trace.footer(
            f"REVIEW {review_id} {message}  ·  "
            f"{outcome.seconds:.1f}s  ·  {outcome.model_calls} model calls"
        )

    if outcome.skipped == SKIP_NO_MODEL:
        done("FAILED  ·  no model configured")
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_FAILED,
            error_code=ERROR_NO_MODEL,
        )
        return

    if not outcome.ran:
        done(f"DONE  ·  {outcome.skipped}")
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_SKIPPED
        )
        return

    stored = await store_findings(session, review_id, outcome.findings)
    logger.info("review %s: %s findings stored", review_id, stored)

    gate = apply_gate(outcome.findings)

    trace.section("GATE", f"{len(gate.post)} to post · {len(gate.hold)} held back")
    for finding, reason in gate.hold:
        trace.item(f"held  {finding.file}:{finding.line}  ({reason})")

    if not gate.post:
        done(f"DONE  ·  {stored} findings, none worth posting")
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_NO_FINDINGS
        )
        return

    await set_review_status(
        session, review_id, expected=STATUS_PROCESSING, new=STATUS_POSTING
    )

    try:
        async with GitHubClient(event.installation_id, client=github_client) as github:
            posted = await post_review(
                github, event.repo_full_name, event.pr_number, event.head_sha,
                gate.post, held_back=len(gate.hold),
            )
    except Exception:
        logger.exception("review %s could not be posted", review_id)
        done("FAILED  ·  could not post comments")
        await set_review_status(
            session, review_id, expected=STATUS_POSTING, new=STATUS_FAILED,
            error_code=ERROR_POST_FAILED,
        )
        return

    matched = await record_posted_review(
        session, review_id, posted.github_review_id, posted.comments
    )

    await set_review_status(
        session, review_id, expected=STATUS_POSTING, new=STATUS_POSTED
    )

    trace.section("POSTED", f"review {posted.github_review_id}")
    trace.item(posted.html_url or "(no url returned)")
    trace.item(f"{matched} of {len(gate.post)} comments matched back to findings")

    done(f"DONE  ·  {len(gate.post)} posted, {len(gate.hold)} held")
