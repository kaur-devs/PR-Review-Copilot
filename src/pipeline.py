from __future__ import annotations

import logging

from src.db.queries import set_review_status, start_attempt
from src.db.session import get_session_factory
from src.diff.files import ChangedFileSet, fetch_changed_files
from src.diff.parse import parse_changed_files
from src.diff.skip import worth_reviewing
from src.github.client import GitHubClient
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

ERROR_FETCH_FAILED = "DIFF_FETCH_FAILED"
ERROR_UNEXPECTED = "PIPELINE_UNEXPECTED"


async def process_pull_request(
    event: PullRequestEvent, review_id: int, *, github_client=None
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
            await _review(session, event, review_id, github_client)
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
    session, event: PullRequestEvent, review_id: int, github_client=None
) -> None:
    async with GitHubClient(event.installation_id, client=github_client) as github:
        file_set = await fetch_changed_files(
            github, event.repo_full_name, event.pr_number
        )

    logger.info(
        "review %s: %s changed %s files (%s lines)%s",
        review_id,
        event.repo_full_name,
        len(file_set),
        file_set.total_changes,
        " [truncated by GitHub]" if file_set.truncated else "",
    )

    keep, skipped = worth_reviewing(file_set.files)

    for filename, reason in skipped.items():
        logger.info("review %s: skipping %s because %s", review_id, filename, reason)

    if not keep:
        logger.info("review %s: nothing worth reviewing", review_id)
        await set_review_status(
            session, review_id, expected=STATUS_PROCESSING, new=STATUS_SKIPPED
        )
        return

    diffs = parse_changed_files(ChangedFileSet(files=keep))

    for diff in diffs:
        if not diff.parsed:
            logger.warning(
                "review %s: could not read the diff for %s (%s)",
                review_id,
                diff.filename,
                diff.parse_error,
            )
            continue

        logger.info(
            "review %s: %s has %s changed lines across %s hunks",
            review_id,
            diff.filename,
            len(diff.added_line_numbers),
            len(diff.hunks),
        )

    readable = [diff for diff in diffs if diff.parsed]
    total_lines = sum(len(diff.added_line_numbers) for diff in readable)

    logger.info(
        "review %s: ready to review %s files, %s changed lines",
        review_id,
        len(readable),
        total_lines,
    )
