"""Which events we act on, and reading the fields out of a payload."""

import pytest

from src.webhooks.events import PullRequestEvent, is_actionable


def test_the_four_actions_that_mean_new_code():
    for action in ["opened", "synchronize", "reopened", "ready_for_review"]:
        assert is_actionable("pull_request", action)


def test_actions_that_change_no_code_are_skipped():
    for action in ["labeled", "assigned", "closed", "edited", "review_requested"]:
        assert not is_actionable("pull_request", action)


def test_other_kinds_of_event_are_skipped():
    assert not is_actionable("push", "opened")
    assert not is_actionable("issues", "opened")
    assert not is_actionable("installation", "created")


def test_a_missing_event_or_action_is_skipped():
    assert not is_actionable(None, "opened")
    assert not is_actionable("pull_request", None)


def test_the_fields_we_need_are_read_out_of_the_payload():
    payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}},
        "repository": {"id": 555, "full_name": "kaur-devs/pr-review-sandbox"},
        "installation": {"id": 999},
    }

    event = PullRequestEvent.from_payload("delivery-abc", payload)

    assert event.delivery_id == "delivery-abc"
    assert event.action == "opened"
    assert event.pr_number == 42
    assert event.repo_id == 555
    assert event.repo_full_name == "kaur-devs/pr-review-sandbox"
    assert event.installation_id == 999
    assert event.head_sha == "a" * 40
    assert event.base_sha == "b" * 40


def test_an_incomplete_payload_says_what_is_missing():
    """GitHub should always send these, but if one is absent we want a clear
    error naming the field rather than a confusing crash later."""
    payload = {"action": "opened", "number": 1, "repository": {}, "installation": {}}

    with pytest.raises(KeyError):
        PullRequestEvent.from_payload("d", payload)
