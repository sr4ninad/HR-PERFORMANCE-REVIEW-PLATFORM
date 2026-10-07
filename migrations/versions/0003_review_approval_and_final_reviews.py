"""Manager approval of nominated reviewers, and manager-written final reviews.

Existing assignments predate approval, so they're backfilled as
status='approved', source='nominated' -- they keep working exactly as before.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMN (no table rebuild): server defaults fill existing rows.
    op.add_column(
        "reviewer_assignments",
        sa.Column("status", sa.String(length=16), server_default="approved", nullable=False),
    )
    op.add_column(
        "reviewer_assignments",
        sa.Column("source", sa.String(length=16), server_default="nominated", nullable=False),
    )
    op.add_column("reviewer_assignments", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "final_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cycle_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("strengths", sa.Text(), nullable=True),
        sa.Column("improvements", sa.Text(), nullable=True),
        sa.Column("final_rating", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stats_snapshot", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["cycle_id"], ["review_cycles.id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cycle_id", "employee_id", name="uq_final_review_cycle_employee"),
    )


def downgrade() -> None:
    op.drop_table("final_reviews")
    with op.batch_alter_table("reviewer_assignments") as batch_op:
        batch_op.drop_column("decided_at")
        batch_op.drop_column("source")
        batch_op.drop_column("status")
