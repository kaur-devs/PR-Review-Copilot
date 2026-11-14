"""index every name in a repository, so retrieval is a lookup

Finding which files mention a changed symbol meant reading every file in
the repository, once per symbol, on every review — 360,000 file reads on
a repository of 14,000 files, recomputed from scratch each time and then
discarded.

Revision ID: c92f481ab7de
Revises: b7e4c1a90f33
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c92f481ab7de"
down_revision: Union[str, None] = "b7e4c1a90f33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "symbol_references",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "repo_id", sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
    )
    # The lookup.
    op.create_index(
        "ix_symbol_references_name", "symbol_references", ["repo_id", "name"]
    )
    # The update. Without this, replacing one changed file's rows scans
    # every row the repository has — the exact cost this table removes.
    op.create_index(
        "ix_symbol_references_file", "symbol_references", ["repo_id", "file_path"]
    )

    op.create_table(
        "repository_indexes",
        sa.Column(
            "repo_id", sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("names", sa.Integer(), server_default="0", nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("repository_indexes")
    op.drop_index("ix_symbol_references_file", table_name="symbol_references")
    op.drop_index("ix_symbol_references_name", table_name="symbol_references")
    op.drop_table("symbol_references")
