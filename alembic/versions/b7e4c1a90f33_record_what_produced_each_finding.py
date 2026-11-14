"""record what produced each finding, and what each review cost

Every quality number this project quotes came from reading a log. Nothing
in the database said which model or which prompt produced a finding, so a
recall figure could not be traced back to the thing that produced it.

Revision ID: b7e4c1a90f33
Revises: a3d71f20b8c4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c1a90f33"
down_revision: Union[str, None] = "a3d71f20b8c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable throughout: reviews 1-23 happened before any of this existed
    # and backfilling them with a guess would be worse than leaving them
    # blank, which at least says "we did not record this".
    op.add_column("findings", sa.Column("message", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("model", sa.String(128), nullable=True))
    op.add_column(
        "findings", sa.Column("prompt_version", sa.String(32), nullable=True)
    )
    op.add_column("findings", sa.Column("found_by", sa.String(32), nullable=True))

    op.add_column("reviews", sa.Column("model_calls", sa.Integer(), nullable=True))
    op.add_column("reviews", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "reviews", sa.Column("completion_tokens", sa.Integer(), nullable=True)
    )
    op.add_column("reviews", sa.Column("seconds", sa.Float(), nullable=True))

    # The question the quota analysis wants answered: what did each model
    # and each prompt version actually find.
    op.create_index(
        "ix_findings_model_prompt", "findings", ["model", "prompt_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_findings_model_prompt", table_name="findings")
    for column in ("seconds", "completion_tokens", "prompt_tokens", "model_calls"):
        op.drop_column("reviews", column)
    for column in ("found_by", "prompt_version", "model", "message"):
        op.drop_column("findings", column)
