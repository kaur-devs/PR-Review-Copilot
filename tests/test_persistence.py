"""What ends up in the database, and what must never end up there."""

import hashlib
import hmac
import json

import pytest
from sqlalchemy import func, select

from src.db.models import Repo, Review, WebhookEvent
from tests.conftest import TEST_SECRET, run_on_database

ENDPOINT = "/webhooks/github"


def rows(*columns):
    """Every row of the chosen columns, as a list of tuples."""
    return run_on_database(lambda connection: connection.execute(select(*columns))).all()


def count(table) -> int:
    """How many rows are in a table."""
    return run_on_database(
        lambda connection: connection.scalar(select(func.count()).select_from(table))
    )


def build_payload(action="opened", pr_number=7, head_sha="a" * 40):
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {"head": {"sha": head_sha}, "base": {"sha": "b" * 40}},
        "repository": {"id": 123456, "full_name": "kaur-devs/pr-review-sandbox"},
        "installation": {"id": 98765},
    }


def post(client, payload, *, event="pull_request", delivery="delivery-1",
         secret=TEST_SECRET):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        ENDPOINT,
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
        },
    )


def test_a_new_pull_request_creates_one_message_and_one_review(client):
    response = post(client, build_payload())
    assert response.json()["status"] == "accepted"

    assert count(WebhookEvent) == 1
    assert count(Review) == 1

    outcome, review_id, head_sha = rows(
        WebhookEvent.outcome, WebhookEvent.review_id, WebhookEvent.head_sha
    )[0]
    assert outcome == "accepted"
    assert review_id is not None     # the message is linked to its review
    assert head_sha == "a" * 40


def test_a_new_review_starts_in_received_with_no_attempts(client):
    post(client, build_payload())

    status, attempts = rows(Review.status, Review.attempts)[0]
    assert status == "received"
    assert attempts == 0


def test_an_event_we_ignore_is_still_written_down(client):
    """We keep a record of every genuine message, even the ones we skip."""
    response = post(client, build_payload(action="labeled"))
    assert response.json()["status"] == "ignored"

    assert count(WebhookEvent) == 1     # recorded
    assert count(Review) == 0           # but nothing to review
    assert rows(WebhookEvent.outcome)[0][0] == "ignored"


def test_the_same_message_twice_is_only_stored_once(client):
    """This is what happens when someone presses Redeliver on GitHub."""
    post(client, build_payload(), delivery="same-id")
    second = post(client, build_payload(), delivery="same-id")

    assert second.json()["status"] == "duplicate"
    assert count(WebhookEvent) == 1
    assert count(Review) == 1


def test_a_new_message_about_an_already_claimed_commit_starts_no_second_review(client):
    """Reopening a pull request sends a fresh message about unchanged code."""
    first = post(client, build_payload(), delivery="d-opened")
    second = post(client, build_payload(action="reopened"), delivery="d-reopened")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"

    assert count(WebhookEvent) == 2     # two genuine messages
    assert count(Review) == 1           # one piece of work


def test_that_second_message_is_recorded_as_a_duplicate_not_an_acceptance(client):
    """The record has to be honest about what actually happened."""
    post(client, build_payload(), delivery="d-opened")
    post(client, build_payload(action="reopened"), delivery="d-reopened")

    outcomes = dict(rows(WebhookEvent.delivery_id, WebhookEvent.outcome))
    assert outcomes["d-opened"] == "accepted"
    assert outcomes["d-reopened"] == "duplicate"


def test_new_commits_start_a_new_review(client):
    """Pushing more code is genuinely new work."""
    post(client, build_payload(head_sha="a" * 40), delivery="d-1")
    post(client, build_payload(action="synchronize", head_sha="c" * 40), delivery="d-2")

    assert count(Review) == 2


def test_nothing_is_written_for_a_message_that_fails_the_signature_check(client):
    """Unverified input must never reach the database at all."""
    response = post(client, build_payload(), secret="not-our-secret")

    assert response.status_code == 401
    assert count(WebhookEvent) == 0
    assert count(Review) == 0
    assert count(Repo) == 0


def test_the_repository_is_stored_once_however_many_events_arrive(client):
    post(client, build_payload(head_sha="a" * 40), delivery="d-1")
    post(client, build_payload(action="synchronize", head_sha="c" * 40), delivery="d-2")

    assert count(Repo) == 1
    assert rows(Repo.full_name)[0][0] == "kaur-devs/pr-review-sandbox"
