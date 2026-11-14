"""drop the name index, which was measured and did not earn its place

Built to find the far end of a renamed symbol in a file the diff does not
contain. It worked: it located a renamed response key's two TypeScript
consumers across the language boundary in 50ms.

It did not help. Six runs of one pull request, three with and three
without, scored 38/31/44% recall against 50/44/38%. The defect it exists
for was found without it, because the specialist prompt already tells the
agent to grep the whole repository for a removed name and the grep tool
does not filter by language. The index handed an agent something it
already had, and the context cost it attention on every turn.

Kept in git rather than in the schema. `git revert` brings it back if a
repository large enough to make the latency case ever arrives.

Revision ID: d1f3a07c45b2
Revises: c92f481ab7de
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3a07c45b2"
down_revision: Union[str, None] = "c92f481ab7de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("repository_indexes")
    op.drop_index("ix_symbol_references_file", table_name="symbol_references")
    op.drop_index("ix_symbol_references_name", table_name="symbol_references")
    op.drop_table("symbol_references")


def downgrade() -> None:
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
    op.create_index(
        "ix_symbol_references_name", "symbol_references", ["repo_id", "name"]
    )
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
