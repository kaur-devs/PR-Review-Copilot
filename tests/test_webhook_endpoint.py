"""The endpoint end to end: a real HTTP request in, a real response out."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.main import app
from tests.conftest import TEST_SECRET

ENDPOINT = "/webhooks/github"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def build_payload(action="opened", pr_number=7):
    """A payload shaped like the real thing, with only the fields we read."""
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}},
        "repository": {"id": 123456, "full_name": "kaur-devs/pr-review-sandbox"},
        "installation": {"id": 98765},
    }


def post(client, payload, *, event="pull_request", delivery="delivery-1",
         secret=TEST_SECRET, drop_header=None):
    """Send a correctly signed request, the way GitHub would."""
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }
    if drop_header:
        del headers[drop_header]
    return client.post(ENDPOINT, content=body, headers=headers)


def test_the_service_reports_itself_healthy(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_a_new_pull_request_is_accepted(client):
    response = post(client, build_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["repository"] == "kaur-devs/pr-review-sandbox"
    assert body["pull_request"] == 7
    assert body["head_sha"] == "a" * 40


def test_a_request_with_no_signature_is_refused(client):
    body = json.dumps(build_payload()).encode()
    response = client.post(
        ENDPOINT,
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_SIGNATURE_INVALID"


def test_a_request_signed_with_the_wrong_secret_is_refused(client):
    response = post(client, build_payload(), secret="not-our-secret")
    assert response.status_code == 401


def test_a_tampered_body_is_refused(client):
    """The signature is valid for one body, but a different body was sent."""
    real_body = json.dumps(build_payload()).encode()
    signature = "sha256=" + hmac.new(
        TEST_SECRET.encode(), real_body, hashlib.sha256
    ).hexdigest()
    tampered_body = json.dumps(build_payload(pr_number=99)).encode()

    response = client.post(
        ENDPOINT,
        content=tampered_body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 401


def test_an_action_we_do_not_review_gets_a_200(client):
    """Not an error. There is simply nothing to do."""
    response = post(client, build_payload(action="labeled"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_an_event_we_do_not_review_gets_a_200(client):
    response = post(client, {"action": "created"}, event="installation")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_a_missing_github_header_is_a_400(client):
    response = post(client, build_payload(), drop_header="X-GitHub-Event")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEBHOOK_HEADERS_MISSING"


def test_a_payload_missing_fields_is_a_400(client):
    payload = build_payload()
    del payload["installation"]

    response = post(client, payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEBHOOK_PAYLOAD_INCOMPLETE"


def test_a_body_that_is_not_json_is_a_400(client):
    body = b"{this is not json"
    signature = "sha256=" + hmac.new(
        TEST_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()

    response = client.post(
        ENDPOINT,
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEBHOOK_PAYLOAD_MALFORMED"
