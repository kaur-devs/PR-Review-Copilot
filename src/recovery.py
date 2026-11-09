"""Picking up reviews whose process died part way through.

Two facts make this necessary. GitHub never retries a delivery, so nothing
arrives to prompt a second attempt. And the uniqueness rule on repository,
pull request and commit refuses a second claim on the same commit, so even a
manual redelivery used to be turned away. A review interrupted by a crash or
a restart was stuck in "processing" for good.

So the service looks for them itself, when it starts.

Running on startup rather than on a timer is a deliberate limit. The common
cause of an interrupted review is the process stopping, and a process that
stops is a process that starts again. What this does not catch is a review
abandoned by a process that is still running, which would need a scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from src.db.queries import (
    StuckReview,
    abandon_review,
    reopen_review,
    stuck_reviews,
)
from src.db.session import get_session_factory
from src.pipeline import process_pull_request

logger = logging.getLogger(__name__)

ERROR_INTERRUPTED = "REVIEW_INTERRUPTED"

# Each resumed review spends model tokens from a budget measured per minute,
# so they are done one at a time and only a few per start. The rest are found
# again next time, which is the point of looking at startup rather than
# keeping a queue.
MAX_RESUMED_PER_START = 5


def recovery_enabled() -> bool:
    return os.getenv("RECOVERY_ON_STARTUP", "1").lower() not in (
        "0", "false", "no", "off"
    )


@dataclass
class RecoveryReport:
    resumed: list[int] = field(default_factory=list)
    abandoned: list[int] = field(default_factory=list)
    left_for_next_time: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{len(self.resumed)} resumed, {len(self.abandoned)} abandoned, "
            f"{len(self.left_for_next_time)} left for next time"
        )


async def _resume_one(stuck: StuckReview, github_client, llm_client) -> bool:
    async with get_session_factory()() as session:
        if not await reopen_review(session, stuck.review_id):
            # Something moved it on between the scan and now. Not ours.
            logger.info("review %s is no longer stuck", stuck.review_id)
            return False

    await process_pull_request(
        stuck.event,
        stuck.review_id,
        github_client=github_client,
        llm_client=llm_client,
    )
    return True


async def recover_interrupted_reviews(
    *, github_client=None, llm_client=None
) -> RecoveryReport:
    """Resume what can be resumed, give up on what cannot."""
    report = RecoveryReport()

    async with get_session_factory()() as session:
        found = await stuck_reviews(session)

    if not found:
        logger.info("no interrupted reviews to pick up")
        return report

    logger.info("found %s interrupted review(s)", len(found))

    for stuck in found:
        if not stuck.can_retry:
            # Out of attempts. Mark it failed so the row stops claiming to be
            # in progress when nothing is working on it.
            async with get_session_factory()() as session:
                if await abandon_review(
                    session, stuck.review_id, error_code=ERROR_INTERRUPTED
                ):
                    report.abandoned.append(stuck.review_id)
            continue

        if len(report.resumed) >= MAX_RESUMED_PER_START:
            report.left_for_next_time.append(stuck.review_id)
            continue

        try:
            if await _resume_one(stuck, github_client, llm_client):
                report.resumed.append(stuck.review_id)
        except asyncio.CancelledError:
            # The service is shutting down. Leaving the review where it is is
            # correct: it is interrupted again, and the next start finds it.
            report.left_for_next_time.append(stuck.review_id)
            raise
        except Exception:
            logger.exception("could not resume review %s", stuck.review_id)

    logger.info("recovery: %s", report)
    return report


def start_recovery() -> asyncio.Task | None:
    """Begin recovery in the background, or do nothing if it is switched off.

    Deliberately not awaited. GitHub allows ten seconds to answer a delivery
    and does not retry, so a service that is slow to start listening loses
    messages outright. Old work must never delay new work.
    """
    if not recovery_enabled():
        logger.info("startup recovery is switched off")
        return None

    return asyncio.create_task(recover_interrupted_reviews())
