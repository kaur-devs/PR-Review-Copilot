from __future__ import annotations

import logging

import time

from src.classification.classify import classify
from src.common import trace
from src.context.gather import gather_context
from src.db.queries import (
    record_posted_review,
    set_review_status,
    start_attempt,
    store_findings,
)
from src.db.session import get_session_factory
from src.diff.files import ChangedFileSet, fetch_changed_files
from src.diff.parse import parse_changed_files
from src.diff.skip import worth_reviewing
from src.filter.gate import apply_gate
from src.github.client import GitHubClient
from src.github.review import post_review
from src.review.generate import generate
from src.review.provider import LLMNotConfigured, LLMProvider
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_SKIPPED = "skipped"
STATUS_NO_FINDINGS = "no_findings"
STATUS_POSTING = "posting"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"

ERROR_FETCH_FAILED = "DIFF_FETCH_FAILED"
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
            await _review(session, event, review_id, github_client, llm_client)
        except Exception:
            logger.exception("review %s failed", review_id)
            await set_review_status(
                session,
                review_id,
                expected=STATUS_PROCESSING,
                new=STATUS_FAILED,
                error_code=ERROR_UNEXPECTED,
            )


async def _review(
    session, event: PullRequestEvent, review_id: int, github_client=None, llm_client=None
) -> None:
    from src.review.provider import quota

    started = time.monotonic()
    calls_before = quota.requests

    trace.banner(
        f"REVIEW {review_id}  ·  {event.repo_full_name}#{event.pr_number}  "
        f"·  {event.head_sha[:7]}"
    )

    async with GitHubClient(event.installation_id, client=github_client) as github:
        file_set = await fetch_changed_files(
            github, event.repo_full_name, event.pr_number
        )

    keep, skipped = worth_reviewing(file_set.files)
    diffs = parse_changed_files(ChangedFileSet(files=keep))
    readable = [diff for diff in diffs if diff.parsed]

    trace.section(
        "CHANGED FILES",
        f"{len(file_set)} changed · {len(keep)} reviewable · {len(skipped)} skipped"
        + (" · TRUNCATED BY GITHUB" if file_set.truncated else ""),
    )
    for changed in keep:
        trace.item(
            f"{changed.filename}   +{changed.additions} -{changed.deletions}"
        )
    for filename, reason in skipped.items():
        trace.item(f"{filename}   skipped: {reason}")

    if readable:
        trace.section("DIFF", f"{sum(len(d.added_line_numbers) for d in readable)} changed lines")
        for diff in readable:
            trace.item(diff.filename)
            trace.diff(diff)

    for diff in diffs:
        if not diff.parsed:
            logger.warning(
                "review %s: could not read the diff for %s (%s)",
                review_id, diff.filename, diff.parse_error,
            )

    if not keep:
        trace.footer(f"REVIEW {review_id} DONE  ·  nothing worth reviewing")
        logger.info("review %s: nothing worth reviewing", review_id)
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_SKIPPED
        )
        return

    context = await gather_context(
        event.repo_full_name,
        event.head_sha,
        event.installation_id,
        readable,
        client=github_client,
    )

    await set_review_status(
        session, review_id, expected=STATUS_PROCESSING, new=STATUS_PROCESSING,
        context_status=context.status,
    )

    trace.section("CHANGED SYMBOLS", f"{len(context.symbols)} found")
    for symbol in context.symbols:
        trace.item(
            f"{symbol.kind:<8} {symbol.qualified_name:<34} "
            f"{symbol.file}:{symbol.start_line}-{symbol.end_line}"
        )

    trace.section(
        "CONNECTED CODE",
        f"{len(context.files)} files · {context.characters} chars · status {context.status}",
    )
    for item in context.items:
        trace.item(f"{item.file}:{item.start_line}-{item.end_line}")
        trace.item(
            f"uses {item.changed_symbol} · {item.relation} · "
            f"{item.discovery} · {item.confidence}",
            level=2,
        )
        trace.snippet(item.snippet)
    for filename, reason in context.problems.items():
        trace.item(f"{filename}   not analysed: {reason}")
    for dropped in context.dropped:
        trace.item(f"{dropped.file}   dropped: over budget")

    try:
        provider = LLMProvider.from_env(client=llm_client)
    except LLMNotConfigured as error:
        trace.footer(f"REVIEW {review_id} FAILED  ·  no model configured")
        logger.warning("review %s cannot run: %s", review_id, error)
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_FAILED,
            error_code=ERROR_NO_MODEL,
        )
        return

    async with provider as model:
        classifications = await classify(model, readable, context.symbols)

        trace.section("CLASSIFICATION", f"{len(classifications)} files")
        for diff in readable:
            label = classifications.get(diff.filename)
            if label:
                trace.item(f"{diff.filename:<44} {label.kind:<12} ({label.source})")

        trace.section("FINDINGS", "")
        findings = await generate(model, readable, context, classifications)

    for finding in findings:
        trace.finding(finding)

    elapsed = time.monotonic() - started
    calls = quota.requests - calls_before

    if not findings:
        trace.footer(
            f"REVIEW {review_id} DONE  ·  no findings  ·  "
            f"{elapsed:.1f}s  ·  {calls} model calls"
        )
        logger.info("review %s: nothing to say", review_id)
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_NO_FINDINGS
        )
        return

    stored = await store_findings(session, review_id, findings)
    logger.info("review %s: %s findings stored", review_id, stored)

    gate = apply_gate(findings)

    trace.section(
        "GATE", f"{len(gate.post)} to post · {len(gate.hold)} held back"
    )
    for finding, reason in gate.hold:
        trace.item(f"held  {finding.file}:{finding.line}  ({reason})")

    if not gate.post:
        trace.footer(
            f"REVIEW {review_id} DONE  ·  {stored} findings, none worth posting  ·  "
            f"{elapsed:.1f}s  ·  {calls} model calls"
        )
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
                github,
                event.repo_full_name,
                event.pr_number,
                event.head_sha,
                gate.post,
                held_back=len(gate.hold),
            )
    except Exception:
        logger.exception("review %s could not be posted", review_id)
        trace.footer(f"REVIEW {review_id} FAILED  ·  could not post comments")
        await set_review_status(
            session, review_id, expected=STATUS_POSTING, new=STATUS_FAILED,
            error_code=ERROR_POST_FAILED,
        )
        return

    matched = await record_posted_review(
        session, review_id, posted.github_review_id, posted.comment_ids
    )

    await set_review_status(
        session, review_id, expected=STATUS_POSTING, new=STATUS_POSTED
    )

    trace.section("POSTED", f"review {posted.github_review_id}")
    trace.item(posted.html_url or "(no url returned)")
    trace.item(f"{matched} of {len(gate.post)} comments matched back to findings")

    elapsed = time.monotonic() - started
    calls = quota.requests - calls_before
    trace.footer(
        f"REVIEW {review_id} DONE  ·  {len(gate.post)} posted, {len(gate.hold)} held  ·  "
        f"{elapsed:.1f}s  ·  {calls} model calls"
    )
