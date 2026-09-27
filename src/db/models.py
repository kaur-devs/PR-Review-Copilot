"""The database tables.

Two ideas drive the whole design.

First, every genuine delivery from GitHub gets a row, including the ones we
ignored and the ones we had already seen. If something goes wrong later, we
can account for every message GitHub sent us.

Second, a "review" means one commit on one pull request. Pushing new code
creates a new review. Reopening a pull request without pushing does not,
because the code has not changed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Every table inherits from this. Alembic reads it to see the schema."""


# The stages a review moves through. Written down here so the database itself
# rejects a typo rather than storing a status nothing understands.
#
#   received     we know about the commit, nothing has started
#   processing   the pipeline is working on it
#   posting      findings are being sent to GitHub
#   posted       comments are live on the pull request
#   skipped      nothing worth reviewing, e.g. only lockfiles changed
#   no_findings  we looked and found nothing to say
#   superseded   newer commits arrived, this review no longer matters
#   failed       something broke; error_code says what
REVIEW_STATUSES = (
    "received", "processing", "posting", "posted",
    "skipped", "no_findings", "superseded", "failed",
)

# What we did with a delivery from GitHub.
#   accepted   we started a review
#   duplicate  we had already seen this message, or already had this commit
#   ignored    a real message, but nothing to review
DELIVERY_OUTCOMES = ("accepted", "duplicate", "ignored")

# How much surrounding code we managed to gather for a review.
#   ok           we downloaded the repository and read it successfully
#   degraded     some files would not parse, so we used weaker matching
#   unavailable  we could not download the code and reviewed the diff alone
CONTEXT_STATUSES = ("ok", "degraded", "unavailable")


class Installation(Base):
    """Someone who installed our App on their account."""

    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(primary_key=True)

    # GitHub's own id for the installation. We need it to ask for permission
    # to read their code.
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)

    # The username or organisation name, e.g. "kaur-devs".
    account_login: Mapped[str] = mapped_column(String(255))

    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    repos: Mapped[list["Repo"]] = relationship(back_populates="installation")


class Repo(Base):
    """A repository we have been given access to."""

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)

    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.id", ondelete="CASCADE"), index=True
    )

    # We match repositories on this number, never on the name. A repository
    # can be renamed at any time; its number never changes.
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True)

    # For example "kaur-devs/pr-review-sandbox". Kept for readability in
    # logs and the dashboard, and refreshed whenever we see a new name.
    full_name: Mapped[str] = mapped_column(String(512))

    default_branch: Mapped[str] = mapped_column(String(255), default="main")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    installation: Mapped["Installation"] = relationship(back_populates="repos")
    reviews: Mapped[list["Review"]] = relationship(back_populates="repo")


class Review(Base):
    """One piece of review work: one commit on one pull request."""

    __tablename__ = "reviews"
    __table_args__ = (
        # The important one. Two messages about the same commit can only ever
        # produce one review, because the database refuses the second. We rely
        # on this instead of checking first in Python, which would be unsafe
        # when two messages arrive at the same moment.
        UniqueConstraint(
            "repo_id", "pr_number", "head_sha", name="uq_reviews_repo_pr_head"
        ),
        # Reject a status that is not in our list.
        CheckConstraint("status IN " + str(REVIEW_STATUSES), name="ck_reviews_status"),
        CheckConstraint(
            "context_status IS NULL OR context_status IN " + str(CONTEXT_STATUSES),
            name="ck_reviews_context_status",
        ),
        # For the dashboard: recent reviews for one repository.
        Index("ix_reviews_repo_created", "repo_id", "created_at"),
        # For finding reviews that got stuck part way through.
        Index("ix_reviews_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    pr_number: Mapped[int] = mapped_column(Integer)

    # The commit we are reviewing. Forty characters because that is the length
    # of a git commit id.
    head_sha: Mapped[str] = mapped_column(String(40))

    status: Mapped[str] = mapped_column(String(32), default="received")

    # How many times we have started this review. Stops us retrying forever.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    # Filled in only when status is "failed".
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # How much surrounding code we managed to gather.
    context_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # GitHub's id for the review we posted, once it accepts one.
    github_review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Updated automatically on every change, which is how we spot a review
    # that has been sitting in one status for too long.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repo: Mapped["Repo"] = relationship(back_populates="reviews")
    findings: Mapped[list["Finding"]] = relationship(back_populates="review")


class WebhookEvent(Base):
    """One row for every genuine message GitHub sent us.

    Messages that fail the signature check are logged but never stored, so
    nothing unverified ever reaches the database.

    We do not store the whole payload, only the fields that identify it. The
    payload contains source code, and keeping it would mean holding other
    people's code for no good reason.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            "outcome IN " + str(DELIVERY_OUTCOMES), name="ck_webhook_events_outcome"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # GitHub's unique id for this message. Unique here too, which is how we
    # spot the same message arriving twice.
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True)

    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Only filled in for pull request events.
    github_repo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    outcome: Mapped[str] = mapped_column(String(16))

    # The review this message started, if it started one.
    review_id: Mapped[int | None] = mapped_column(
        ForeignKey("reviews.id", ondelete="SET NULL"), nullable=True
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Finding(Base):
    """One thing the reviewer wants to say about one line of code."""

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )

    file: Mapped[str] = mapped_column(String(1024))
    line: Mapped[int] = mapped_column(Integer)

    severity: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(64))

    # The explanation shown to the developer.
    rationale: Mapped[str] = mapped_column(Text)

    # How sure we are, between 0 and 1. Findings below a threshold are kept
    # in the database but never posted, so we can tune the threshold later
    # without running the reviewer again.
    confidence: Mapped[float] = mapped_column(Float)

    posted: Mapped[bool] = mapped_column(Boolean, default=False)

    # GitHub's id for the comment, so we can later check whether the developer
    # resolved it or dismissed it.
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    review: Mapped["Review"] = relationship(back_populates="findings")


class Outcome(Base):
    """What the developer did with one of our comments.

    This is how the project measures whether it is actually useful, rather
    than just claiming to be.
    """

    __tablename__ = "outcomes"
    __table_args__ = (Index("ix_outcomes_repo_category", "repo_id", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE")
    )
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(64))

    # accepted (they acted on it), rejected (they dismissed it), or ignored.
    outcome: Mapped[str] = mapped_column(String(16))

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RepoSuppression(Base):
    """A kind of comment one repository has asked us to stop making.

    If a team keeps dismissing our comments about, say, naming, we stop making
    them for that team.
    """

    __tablename__ = "repo_suppressions"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[str] = mapped_column(String(64), primary_key=True)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
