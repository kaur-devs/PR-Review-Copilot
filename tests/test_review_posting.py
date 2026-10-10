import json

import httpx
import pytest
from sqlalchemy import select

from src.db.models import Finding as FindingRow
from src.db.models import Review
from src.db.queries import claim_review, ensure_repo, record_posted_review, store_findings
from src.db.session import get_session_factory
from src.github import auth
from src.github.client import GitHubClient
from src.github.review import (
    build_comment_body,
    build_comments,
    build_review_body,
    post_review,
)
from src.review.findings import Finding
from src.webhooks.events import PullRequestEvent
from tests.fakes import fake_github

EVENT = PullRequestEvent(
    delivery_id="d-1", action="opened", installation_id=42, repo_id=7,
    repo_full_name="kaur-devs/sandbox", pr_number=3,
    head_sha="a" * 40, base_sha="b" * 40,
)


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


def make(severity="high", line=10, confidence=0.9, evidence=()):
    return Finding(
        file="app/models.py", line=line, severity=severity, category="correctness",
        message="Callers assume a user is returned",
        rationale="service.py reads user.email straight after calling this.",
        confidence=confidence, evidence_files=evidence,
    )


# ---------------------------------------------------------------------------
# What a comment looks like
# ---------------------------------------------------------------------------

def test_a_comment_leads_with_severity_and_category():
    body = build_comment_body(make())

    assert body.startswith("**high · correctness**")


def test_a_comment_carries_the_message_and_the_reasoning():
    body = build_comment_body(make())

    assert "Callers assume a user is returned" in body
    assert "service.py reads user.email" in body


def test_a_comment_names_the_other_files_that_were_consulted():
    body = build_comment_body(make(evidence=("app/service.py", "app/reports.py")))

    assert "app/service.py" in body
    assert "app/reports.py" in body


def test_a_comment_shows_its_confidence():
    assert "confidence 0.9" in build_comment_body(make(confidence=0.9))


def test_the_rationale_is_not_repeated_when_it_matches_the_message():
    finding = Finding("a.py", 1, "high", "correctness", "same text", "same text", 0.9)

    assert build_comment_body(finding).count("same text") == 1


# ---------------------------------------------------------------------------
# What the review summary says
# ---------------------------------------------------------------------------

def test_the_summary_counts_by_severity():
    body = build_review_body([make("high"), make("high", line=2), make("medium", line=3)], 0)

    assert "3 issue(s)" in body
    assert "2 high" in body
    assert "1 medium" in body


def test_the_summary_mentions_findings_that_were_held_back():
    body = build_review_body([make()], held=4)

    assert "4 further finding(s)" in body


def test_a_review_with_nothing_to_say_says_so():
    assert build_review_body([], 0) == "No issues found."


# ---------------------------------------------------------------------------
# The shape GitHub requires
# ---------------------------------------------------------------------------

def test_each_comment_is_anchored_to_a_file_line_and_side():
    comments = build_comments([make(line=42)])

    assert comments[0]["path"] == "app/models.py"
    assert comments[0]["line"] == 42
    assert comments[0]["side"] == "RIGHT"


def test_comments_use_line_and_side_rather_than_the_deprecated_position():
    comments = build_comments([make()])

    assert "position" not in comments[0]


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def test_a_review_is_posted_as_a_comment_never_as_a_block():
    """ADR-005: the tool comments, it never blocks a merge."""
    client, calls = fake_github([[]])

    async with GitHubClient(42, client=client) as github:
        await post_review(github, "o/r", 3, "a" * 40, [make()])

    post = [c for c in calls if c.method == "POST" and c.url.path.endswith("/reviews")][0]
    body = json.loads(post.content)
    assert body["event"] == "COMMENT"


async def test_the_commit_being_reviewed_is_named():
    client, calls = fake_github([[]])

    async with GitHubClient(42, client=client) as github:
        await post_review(github, "o/r", 3, "c" * 40, [make()])

    post = [c for c in calls if c.method == "POST" and c.url.path.endswith("/reviews")][0]
    assert json.loads(post.content)["commit_id"] == "c" * 40


async def test_every_finding_becomes_one_comment_in_one_review():
    """One review, one notification, however many findings."""
    client, calls = fake_github([[]])
    findings = [make(line=n) for n in range(1, 6)]

    async with GitHubClient(42, client=client) as github:
        await post_review(github, "o/r", 3, "a" * 40, findings)

    posts = [c for c in calls if c.method == "POST" and c.url.path.endswith("/reviews")]
    assert len(posts) == 1
    assert len(json.loads(posts[0].content)["comments"]) == 5


async def test_an_absurd_number_of_findings_is_capped():
    from src.github import review as review_module

    client, calls = fake_github([[]])
    findings = [make(line=n) for n in range(1, 60)]

    async with GitHubClient(42, client=client) as github:
        await post_review(github, "o/r", 3, "a" * 40, findings)

    post = [c for c in calls if c.method == "POST" and c.url.path.endswith("/reviews")][0]
    sent = json.loads(post.content)["comments"]
    assert len(sent) == review_module.MAX_COMMENTS_PER_REVIEW


async def test_the_comment_ids_are_fetched_afterwards():
    """GitHub does not return them when creating the review, so we ask."""
    client, calls = fake_github([[]])

    async with GitHubClient(42, client=client) as github:
        posted = await post_review(github, "o/r", 3, "a" * 40, [make(line=10)])

    assert posted.github_review_id == 909090
    assert posted.comment_ids == {("app/models.py", 10): 7000}

    follow_up = [c for c in calls if c.url.path.endswith("/comments")]
    assert len(follow_up) == 1


# ---------------------------------------------------------------------------
# Recording what was posted
# ---------------------------------------------------------------------------

async def test_posted_findings_are_marked_and_linked():
    async with get_session_factory()() as session:
        repo_id = await ensure_repo(session, EVENT)
        review_id = await claim_review(session, repo_id, EVENT)
        await store_findings(session, review_id, [make(line=10), make(line=20)])

        matched = await record_posted_review(
            session, review_id, 909090, {("app/models.py", 10): 7001}
        )

        rows = (await session.execute(
            select(FindingRow.line, FindingRow.posted, FindingRow.github_comment_id)
            .where(FindingRow.review_id == review_id).order_by(FindingRow.line)
        )).all()

        review = (await session.execute(
            select(Review.github_review_id).where(Review.id == review_id)
        )).scalar_one()

    assert matched == 1
    assert rows == [(10, True, 7001), (20, False, None)]
    assert review == 909090
