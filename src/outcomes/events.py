from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REVIEW_THREAD_EVENT = "pull_request_review_thread"

ACTION_RESOLVED = "resolved"
ACTION_UNRESOLVED = "unresolved"
THREAD_ACTIONS = frozenset({ACTION_RESOLVED, ACTION_UNRESOLVED})

OUTCOME_ACCEPTED = "accepted"


@dataclass(frozen=True)
class ThreadEvent:
    delivery_id: str
    action: str
    repo_id: int
    repo_full_name: str
    pr_number: int
    comment_ids: tuple[int, ...]

    @property
    def resolved(self) -> bool:
        return self.action == ACTION_RESOLVED


def is_thread_event(event: str | None, action: str | None) -> bool:
    return event == REVIEW_THREAD_EVENT and action in THREAD_ACTIONS


def _comment_ids_from(thread: dict) -> tuple[int, ...]:
    comments = thread.get("comments")

    if not isinstance(comments, list):
        logger.warning(
            "the review thread had no comments list. keys present: %s",
            sorted(thread.keys()),
        )
        return ()

    ids = []
    for comment in comments:
        if isinstance(comment, dict) and isinstance(comment.get("id"), int):
            ids.append(comment["id"])

    if not ids:
        logger.warning(
            "none of the %s thread comments had a numeric id. first comment keys: %s",
            len(comments),
            sorted(comments[0].keys()) if comments and isinstance(comments[0], dict) else "n/a",
        )

    return tuple(ids)


def read_thread_event(delivery_id: str, payload: dict) -> ThreadEvent | None:
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        logger.warning("a review thread event arrived with no thread object")
        return None

    try:
        repository = payload["repository"]
        pull_request = payload["pull_request"]
    except KeyError as missing:
        logger.warning("a review thread event was missing %s", missing)
        return None

    comment_ids = _comment_ids_from(thread)
    if not comment_ids:
        return None

    return ThreadEvent(
        delivery_id=delivery_id,
        action=payload["action"],
        repo_id=repository["id"],
        repo_full_name=repository["full_name"],
        pr_number=pull_request["number"],
        comment_ids=comment_ids,
    )
