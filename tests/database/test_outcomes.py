import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from src.db.models import Finding as FindingRow
from src.db.models import Outcome
from src.db.queries import claim_review, ensure_repo, store_findings
from src.db.session import get_session_factory
from src.outcomes.events import is_thread_event, read_thread_event
from src.outcomes.record import record_thread_outcome
from src.review.findings import Finding
from src.webhooks.events import PullRequestEvent
from tests.conftest import TEST_SECRET

EVENT = PullRequestEvent(
    delivery_id="d-1", action="opened", installation_id=42, repo_id=987654,
    repo_full_name="kaur-devs/sandbox", pr_number=3,
    head_sha="a" * 40, base_sha="b" * 40,
)

ENDPOINT = "/webhooks/github"


def thread_payload(action="resolved", comment_ids=(7001,), repo_id=987654):
    return {
        "action": action,
        "thread": {
            "comments": [
                {"id": cid, "path": "app/models.py", "line": 10}
                for cid in comment_ids
            ]
        },
        "pull_request": {"number": 3},
        "repository": {"id": repo_id, "full_name": "kaur-devs/sandbox"},
        "installation": {"id": 42},
    }


async def seed_posted_finding(comment_id=7001, category="correctness"):
    async with get_session_factory()() as session:
        repo_id = await ensure_repo(session, EVENT)
        review_id = await claim_review(session, repo_id, EVENT)
        await store_findings(session, review_id, [Finding(
            file="app/models.py", line=10, severity="high", category=category,
            message="m", rationale="r", confidence=0.9,
        )])
        finding = (await session.execute(
            select(FindingRow).where(FindingRow.review_id == review_id)
        )).scalars().one()
        finding.posted = True
        finding.github_comment_id = comment_id
        await session.commit()
        return finding.id


async def outcomes_for(finding_id):
    async with get_session_factory()() as session:
        return (await session.execute(
            select(Outcome.outcome, Outcome.category)
            .where(Outcome.finding_id == finding_id)
        )).all()


# ---------------------------------------------------------------------------
# Recognising the event
# ---------------------------------------------------------------------------

def test_resolved_and_unresolved_threads_are_acted_on():
    assert is_thread_event("pull_request_review_thread", "resolved")
    assert is_thread_event("pull_request_review_thread", "unresolved")


def test_other_events_are_not_mistaken_for_threads():
    assert not is_thread_event("pull_request", "resolved")
    assert not is_thread_event("pull_request_review_thread", "edited")


def test_the_comment_ids_are_read_out_of_the_thread():
    thread = read_thread_event("d", thread_payload(comment_ids=(1, 2, 3)))

    assert thread.comment_ids == (1, 2, 3)
    assert thread.resolved
    assert thread.pr_number == 3


def test_an_unresolved_thread_is_not_marked_resolved():
    assert not read_thread_event("d", thread_payload(action="unresolved")).resolved


def test_a_thread_with_no_comments_is_reported_not_crashed():
    payload = thread_payload()
    payload["thread"] = {}

    assert read_thread_event("d", payload) is None


def test_a_payload_with_no_thread_at_all_is_handled():
    payload = thread_payload()
    del payload["thread"]

    assert read_thread_event("d", payload) is None


def test_comments_without_numeric_ids_are_ignored():
    payload = thread_payload()
    payload["thread"]["comments"] = [{"node_id": "abc"}]

    assert read_thread_event("d", payload) is None


# ---------------------------------------------------------------------------
# Recording what happened
# ---------------------------------------------------------------------------

async def test_resolving_our_comment_records_it_as_accepted():
    finding_id = await seed_posted_finding(7001)

    async with get_session_factory()() as session:
        result = await record_thread_outcome(
            session, read_thread_event("d", thread_payload(comment_ids=(7001,)))
        )

    assert result.matched == 1
    assert await outcomes_for(finding_id) == [("accepted", "correctness")]


async def test_reopening_the_thread_removes_the_outcome():
    finding_id = await seed_posted_finding(7001)

    async with get_session_factory()() as session:
        await record_thread_outcome(
            session, read_thread_event("d", thread_payload("resolved", (7001,)))
        )
        await record_thread_outcome(
            session, read_thread_event("d", thread_payload("unresolved", (7001,)))
        )

    assert await outcomes_for(finding_id) == []


async def test_resolving_twice_leaves_one_row():
    finding_id = await seed_posted_finding(7001)

    async with get_session_factory()() as session:
        await record_thread_outcome(
            session, read_thread_event("d", thread_payload("resolved", (7001,)))
        )
        await record_thread_outcome(
            session, read_thread_event("d", thread_payload("resolved", (7001,)))
        )

    assert len(await outcomes_for(finding_id)) == 1


async def test_somebody_elses_comment_thread_is_left_alone():
    await seed_posted_finding(7001)

    async with get_session_factory()() as session:
        result = await record_thread_outcome(
            session, read_thread_event("d", thread_payload(comment_ids=(9999,)))
        )

    assert result.matched == 0
    assert result.unknown == 1


async def test_a_repository_we_never_reviewed_is_ignored():
    await seed_posted_finding(7001)

    async with get_session_factory()() as session:
        result = await record_thread_outcome(
            session,
            read_thread_event("d", thread_payload(comment_ids=(7001,), repo_id=11111)),
        )

    assert result.matched == 0


async def test_a_thread_mixing_our_comment_with_replies_still_records_ours():
    finding_id = await seed_posted_finding(7001)

    async with get_session_factory()() as session:
        result = await record_thread_outcome(
            session,
            read_thread_event("d", thread_payload(comment_ids=(7001, 8002, 8003))),
        )

    assert result.matched == 1
    assert result.unknown == 2
    assert await outcomes_for(finding_id) == [("accepted", "correctness")]


# ---------------------------------------------------------------------------
# Through the endpoint
# ---------------------------------------------------------------------------

def post(client, payload, event="pull_request_review_thread", delivery="t-1"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(
        TEST_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return client.post(ENDPOINT, content=body, headers={
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature,
    })


def test_the_endpoint_records_a_resolved_thread(client):
    import asyncio

    finding_id = asyncio.run(seed_posted_finding(7001))

    response = post(client, thread_payload(comment_ids=(7001,)))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["recorded"] == 1
    assert asyncio.run(outcomes_for(finding_id)) == [("accepted", "correctness")]


def test_a_thread_about_comments_we_did_not_write_is_ignored(client):
    response = post(client, thread_payload(comment_ids=(4242,)))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
