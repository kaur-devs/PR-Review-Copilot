"""The database tables.

Design note: docs/30_Design_Notes.md#src-db-models
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


# Listed so the database rejects a typo rather than storing it.
REVIEW_STATUSES = (
    "received", "processing", "posting", "posted",
    "skipped", "no_findings", "all_repeats", "superseded", "failed",
)

# "duplicate" covers the same message twice and the same commit twice.
DELIVERY_OUTCOMES = ("accepted", "duplicate", "ignored")

# "degraded": some files would not parse. "unavailable": no snapshot at all.
CONTEXT_STATUSES = ("ok", "degraded", "unavailable")


class Installation(Base):
    """Someone who installed our App on their account."""

    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(primary_key=True)

    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)

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

    # Matched on the number, never the name: a repository can be renamed.
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True)

    # Refreshed whenever we see a new name, since repositories get renamed.
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
        # Two messages, one review. Checking in Python instead would race.
        UniqueConstraint(
            "repo_id", "pr_number", "head_sha", name="uq_reviews_repo_pr_head"
        ),
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

    # Forty characters, the length of a git commit id.
    head_sha: Mapped[str] = mapped_column(String(40))

    status: Mapped[str] = mapped_column(String(32), default="received")

    # How many times we have started this review. Stops us retrying forever.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    # Filled in only when status is "failed".
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    context_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    github_review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # What the review cost. Previously these lived in a process-global
    # counter and a log line, so "requests per review" was an estimate
    # rather than the query the quota analysis says it is.
    model_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # How we spot a review stuck in one status for too long.
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

    # Unique here too, which is how the same message twice is spotted.
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True)

    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Only filled in for pull request events.
    github_repo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    outcome: Mapped[str] = mapped_column(String(16))

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

    # The sentence the developer actually read. Stored beside the reasoning
    # rather than folded into it, so a finding can still be understood after
    # the fact without guessing which half of the column it was.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str] = mapped_column(Text)

    # Kept below the threshold but never posted, so it can be retuned later.
    confidence: Mapped[float] = mapped_column(Float)

    # Which model, which prompt, which reviewer. Without these, a recall or
    # precision number cannot be traced to what produced it, and "measured,
    # not claimed" is just a slogan.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    found_by: Mapped[str | None] = mapped_column(String(32), nullable=True)

    posted: Mapped[bool] = mapped_column(Boolean, default=False)

    # So we can later see whether the developer resolved or dismissed it.
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    review: Mapped["Review"] = relationship(back_populates="findings")


class Outcome(Base):
    """What the developer did with one of our comments.

    This is how the project measures whether it is actually useful, rather
    than just claiming to be.
    """

    __tablename__ = "outcomes"
    __table_args__ = (
        # A thread can be reopened, so the row is replaced, not appended to.
        UniqueConstraint("finding_id", name="uq_outcomes_finding"),
        Index("ix_outcomes_repo_category", "repo_id", "category"),
    )

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
