"""Falling silent on a category a repository keeps ignoring.

US-006 has read "not built" since the first version of the product
requirements, with the table and the index already in place for it.
These tests pin what counts as evidence, because the obvious reading is
wrong: GitHub never tells us a comment was rejected, so an acceptance
rate over stored rows alone would always be 100%.
"""

import pytest
from sqlalchemy import select

from src.db.models import Finding as FindingRow
from src.db.models import Outcome
from src.db.queries import claim_review, ensure_repo, store_findings
from src.db.session import get_session_factory
from src.filter.suppress import (
    ACCEPTANCE_FLOOR,
    MINIMUM_POSTED,
    apply,
    suppressed_categories,
)
from src.review.findings import Finding
from src.webhooks.events import PullRequestEvent


def an_event(pr_number: int) -> PullRequestEvent:
    return PullRequestEvent(
        delivery_id=f"d-{pr_number}", action="opened", installation_id=42,
        repo_id=987654, repo_full_name="kaur-devs/sandbox",
        pr_number=pr_number, head_sha=f"{pr_number:040d}", base_sha="b" * 40,
    )


def a_finding(category: str, line: int) -> Finding:
    return Finding(
        file="app/models.py", line=line, severity="medium", category=category,
        message="m", rationale="r", confidence=0.9,
    )


async def seed(category: str, posted: int, resolved: int) -> int:
    """A repository's history: findings posted, some of them acted on."""
    async with get_session_factory()() as session:
        repo_id = await ensure_repo(session, an_event(1))
        review_id = await claim_review(session, repo_id, an_event(1))
        await store_findings(
            session, review_id,
            [a_finding(category, line) for line in range(1, posted + 1)],
        )
        rows = (await session.execute(
            select(FindingRow).where(FindingRow.review_id == review_id)
        )).scalars().all()

        for row in rows:
            row.posted = True
        for row in rows[:resolved]:
            session.add(Outcome(
                finding_id=row.id, repo_id=repo_id,
                category=category, outcome="accepted",
            ))
        await session.commit()
        return repo_id


async def test_a_category_nobody_acts_on_goes_quiet():
    repo_id = await seed("maintainability", posted=MINIMUM_POSTED + 2, resolved=1)

    async with get_session_factory()() as session:
        quiet = await suppressed_categories(session, repo_id)

    assert "maintainability" in quiet
    assert "were acted on" in quiet["maintainability"]


async def test_a_category_people_do_act_on_keeps_speaking():
    repo_id = await seed("correctness", posted=MINIMUM_POSTED + 2, resolved=10)

    async with get_session_factory()() as session:
        assert await suppressed_categories(session, repo_id) == {}


async def test_two_ignored_comments_is_a_quiet_week_not_a_verdict():
    """Suppression on a handful of findings would silence a category
    before it had said anything."""
    repo_id = await seed("performance", posted=3, resolved=0)

    async with get_session_factory()() as session:
        assert await suppressed_categories(session, repo_id) == {}


async def test_the_denominator_is_what_was_posted_not_what_was_stored():
    """GitHub has no "this was wrong" event, so a rejection is never
    written down. Counting only stored outcomes would make every
    acceptance rate 100% and nothing would ever be suppressed."""
    repo_id = await seed("performance", posted=MINIMUM_POSTED + 5, resolved=0)

    async with get_session_factory()() as session:
        quiet = await suppressed_categories(session, repo_id)

    assert "performance" in quiet, "no outcomes at all is the strongest signal"


def test_a_suppressed_finding_is_held_not_deleted():
    """Stored either way, so turning the threshold back down recovers it
    without re-running the reviewer."""
    findings = [a_finding("maintainability", 1), a_finding("security", 2)]

    keep, held = apply(findings, {"maintainability": "nobody acts on these"})

    assert [f.category for f in keep] == ["security"]
    assert [f.category for f, _ in held] == ["maintainability"]
    assert held[0][1] == "nobody acts on these"


def test_a_repository_with_no_history_hears_everything():
    findings = [a_finding("maintainability", 1)]

    keep, held = apply(findings, {})

    assert keep == findings and held == []
