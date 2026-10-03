"""The address GitHub sends events to.

GitHub gives us ten seconds to reply and never tries again if we fail. So this
does as little as possible: check the message is genuine, decide whether we
care, and reply. The actual review starts afterwards, once the reply has
already been sent.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.queries import claim_review, ensure_repo, record_delivery, update_delivery
from src.db.session import get_session
from src.pipeline import process_pull_request
from src.webhooks.events import PullRequestEvent, is_actionable
from src.webhooks.signature import SIGNATURE_HEADER, is_valid_signature

logger = logging.getLogger(__name__)

router = APIRouter()

# Headers GitHub sends with every delivery.
EVENT_HEADER = "X-GitHub-Event"        # what kind of event, e.g. pull_request
DELIVERY_HEADER = "X-GitHub-Delivery"  # a unique id for this message


def accepted(status: str, **extra: object) -> JSONResponse:
    """Reply 200 with a short explanation of what we did.

    Every genuine delivery gets a 200, even ones we ignored. GitHub treats any
    other status as a failed delivery, so saying "this was a duplicate" with an
    error code would make correct behaviour look broken.
    """
    return JSONResponse(status_code=200, content={"status": status, **extra})


def rejected(http_status: int, code: str, message: str) -> JSONResponse:
    """Reply with an error, in the shape described in the API specification."""
    return JSONResponse(
        status_code=http_status,
        content={
            "error": {
                "code": code,
                "message": message,
                "requestId": f"req_{uuid.uuid4().hex[:12]}",
            }
        },
    )


@router.post("/webhooks/github")
async def receive_github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    # Read the exact bytes GitHub sent. Do not use request.json() here: that
    # gives us a Python dictionary, and turning it back into text produces
    # different bytes, so the signature check would always fail.
    raw_body = await request.body()

    # Step 1: is this really from GitHub?
    signature = request.headers.get(SIGNATURE_HEADER)
    if not is_valid_signature(raw_body, signature, get_settings().webhook_secret):
        # Vague on purpose. Anyone can send us a message, and someone trying
        # to forge one should learn nothing about why it failed.
        logger.warning("rejected a delivery with an invalid signature")
        return rejected(
            401, "WEBHOOK_SIGNATURE_INVALID", "The request could not be verified."
        )

    # Step 2: does it have the headers we need?
    event = request.headers.get(EVENT_HEADER)
    delivery_id = request.headers.get(DELIVERY_HEADER)
    if not event or not delivery_id:
        return rejected(
            400, "WEBHOOK_HEADERS_MISSING", "Required GitHub headers are absent."
        )

    # Step 3: can we read the body?
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return rejected(
            400, "WEBHOOK_PAYLOAD_MALFORMED", "The request body is not valid JSON."
        )

    # Step 4: is this something we review? If it is, pull out the fields we
    # need. We do this before writing anything, so the row we write already
    # knows what we decided.
    action = payload.get("action")
    pull_request = None
    if is_actionable(event, action):
        try:
            pull_request = PullRequestEvent.from_payload(delivery_id, payload)
        except KeyError as missing:
            logger.error("payload missing %s (delivery %s)", missing, delivery_id)
            return rejected(
                400, "WEBHOOK_PAYLOAD_INCOMPLETE", f"Payload is missing {missing}."
            )

    # Step 5: write down that we received it. If this message has been seen
    # before, we get nothing back and stop here.
    delivery_row_id = await record_delivery(
        session,
        delivery_id=delivery_id,
        event=event,
        action=action,
        outcome="accepted" if pull_request else "ignored",
        pull_request=pull_request,
    )

    if delivery_row_id is None:
        logger.info("already seen delivery %s (%s/%s)", delivery_id, event, action)
        return accepted("duplicate", delivery=delivery_id)

    if pull_request is None:
        logger.info("ignoring %s/%s (delivery %s)", event, action, delivery_id)
        return accepted("ignored", event=event, action=action)

    # Step 6: claim the commit. A different message may have claimed it
    # already, for example when a pull request is reopened without any new
    # code being pushed.
    repo_id = await ensure_repo(session, pull_request)
    review_id = await claim_review(session, repo_id, pull_request)

    if review_id is None:
        # This message was worth acting on, but there was nothing left to do.
        # Correct what we wrote a moment ago, so the record is honest.
        await update_delivery(session, delivery_row_id, outcome="duplicate")
        logger.info("commit %s is already being reviewed", pull_request.head_sha[:7])
        return accepted("duplicate", reason="commit already claimed")

    # Link the message to the review it started.
    await update_delivery(session, delivery_row_id, review_id=review_id)

    # Step 7: hand the work off. A background task runs after this reply has
    # been sent, which is what keeps us inside GitHub's ten second limit.
    background_tasks.add_task(process_pull_request, pull_request, review_id)

    logger.info(
        "accepted %s#%s %s as review %s (delivery %s)",
        pull_request.repo_full_name,
        pull_request.pr_number,
        action,
        review_id,
        delivery_id,
    )
    return accepted(
        "accepted",
        review_id=review_id,
        repository=pull_request.repo_full_name,
        pull_request=pull_request.pr_number,
        head_sha=pull_request.head_sha,
    )
