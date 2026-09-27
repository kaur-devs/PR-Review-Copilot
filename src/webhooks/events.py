"""Deciding which GitHub events we care about, and reading the useful bits.

GitHub sends every event we subscribed to, plus a few we cannot turn off, all
to the same address. Most of them are not something to review, so the
filtering happens here in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

# The only event type we review.
PULL_REQUEST_EVENT = "pull_request"

# A pull request event also carries an "action" saying what happened. These
# four mean there is new code to look at:
#
#   opened            someone created the pull request
#   synchronize       someone pushed more commits to it
#   reopened          a closed pull request was reopened
#   ready_for_review  a draft became a real pull request
#
# Everything else (labelled, assigned, commented on, closed) changes no code.
ACTIONS_WE_REVIEW = frozenset(
    {"opened", "synchronize", "reopened", "ready_for_review"}
)


def is_actionable(event: str | None, action: str | None) -> bool:
    """True if this event means there is code to review."""
    return event == PULL_REQUEST_EVENT and action in ACTIONS_WE_REVIEW


@dataclass(frozen=True)
class PullRequestEvent:
    """The handful of fields we need, pulled out of a very large payload.

    GitHub's message is thousands of lines. Everything after this point works
    from this small object instead, which has two benefits: the rest of the
    code is easy to read, and the review pipeline can be driven without a
    webhook at all. That last point matters because the evaluation harness
    replays old pull requests, where no webhook exists.
    """

    # GitHub's unique id for this delivery. Used to spot the same message twice.
    delivery_id: str

    # What happened: opened, synchronize, and so on.
    action: str

    # Which installation of our App this came from. Needed to get permission
    # to read the code.
    installation_id: int

    # GitHub's numeric id for the repository. We key on this rather than the
    # name, because a repository can be renamed and its id cannot change.
    repo_id: int

    # For example "kaur-devs/pr-review-sandbox".
    repo_full_name: str

    # The pull request number shown in the GitHub interface.
    pr_number: int

    # The newest commit on the pull request. This is the code we review.
    head_sha: str

    # The commit the pull request branches from. Needed to work out the diff.
    base_sha: str

    @classmethod
    def from_payload(cls, delivery_id: str, payload: dict) -> "PullRequestEvent":
        """Pick the fields we need out of GitHub's message.

        Raises KeyError if anything expected is missing, which the endpoint
        turns into a clear error rather than a crash.
        """
        pull_request = payload["pull_request"]
        return cls(
            delivery_id=delivery_id,
            action=payload["action"],
            installation_id=payload["installation"]["id"],
            repo_id=payload["repository"]["id"],
            repo_full_name=payload["repository"]["full_name"],
            pr_number=payload["number"],
            head_sha=pull_request["head"]["sha"],
            base_sha=pull_request["base"]["sha"],
        )
