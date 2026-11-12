"""drop the unused suppression table

The table was created with the initial schema for a feature that was never
built: nothing in the codebase ever read or wrote a row. Its foreign key
still held `repos` in place, so the dead weight was not free.

Rebuilding it is this migration run backwards, so nothing is lost by
removing it now and adding it back when suppression is actually written.

Revision ID: a3d71f20b8c4
Revises: c41a7b9e2d08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3d71f20b8c4"
down_revision: Union[str, Sequence[str], None] = "c41a7b9e2d08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("repo_suppressions")


def downgrade() -> None:
    op.create_table(
        "repo_suppressions",
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repo_id", "category"),
    )
