"""allow the all_repeats status

Revision ID: c41a7b9e2d08
Revises: fe8b57ce506e
Create Date: 2026-10-24 11:02:17.000000

A re-review whose every finding was already posted is not the same thing as
a review that found nothing, and the difference is worth being able to count.
The status list is enforced by a CHECK constraint, so adding one means
replacing the constraint rather than only editing the Python tuple.
"""
from typing import Sequence, Union

from alembic import op

from src.db.models import REVIEW_STATUSES

# revision identifiers, used by Alembic.
revision: str = 'c41a7b9e2d08'
down_revision: Union[str, Sequence[str], None] = 'fe8b57ce506e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WITHOUT_ALL_REPEATS = tuple(s for s in REVIEW_STATUSES if s != "all_repeats")


def upgrade() -> None:
    op.drop_constraint('ck_reviews_status', 'reviews', type_='check')
    op.create_check_constraint(
        'ck_reviews_status', 'reviews', "status IN " + str(REVIEW_STATUSES)
    )


def downgrade() -> None:
    # Any row already holding the new status would fail the old constraint, so
    # it is moved to the nearest thing the old list understands.
    op.execute("UPDATE reviews SET status = 'no_findings' WHERE status = 'all_repeats'")
    op.drop_constraint('ck_reviews_status', 'reviews', type_='check')
    op.create_check_constraint(
        'ck_reviews_status', 'reviews', "status IN " + str(WITHOUT_ALL_REPEATS)
    )
