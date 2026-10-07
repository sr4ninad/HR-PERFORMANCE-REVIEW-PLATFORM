"""Initial schema: users, cycles, assignments, reviews, audit log.

This is the schema the app originally bootstrapped with
Base.metadata.create_all(). Databases created that way (before Alembic)
have no alembic_version table; app/db_migrations.py detects them and
stamps them at this revision instead of trying to re-create the tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SKILL_AREAS = ["work_quality", "productivity", "communication", "collaboration", "initiative", "punctuality"]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "review_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "reviewer_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cycle_id", sa.Integer(), nullable=False),
        sa.Column("reviewee_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cycle_id"], ["review_cycles.id"]),
        sa.ForeignKeyConstraint(["reviewee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cycle_id", "reviewee_id", "reviewer_id", name="uq_assignment_triplet"),
    )

    skill_columns = []
    for area in _SKILL_AREAS:
        skill_columns.append(sa.Column(f"{area}_text", sa.Text(), nullable=True))
        skill_columns.append(sa.Column(f"{area}_rating", sa.Integer(), nullable=True))

    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        *skill_columns,
        sa.Column("additional_notes", sa.Text(), nullable=True),
        sa.Column("overall_rating", sa.Float(), nullable=True),
        sa.Column("is_locked", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["reviewer_assignments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assignment_id"),
    )


def downgrade() -> None:
    op.drop_table("reviews")
    op.drop_table("reviewer_assignments")
    op.drop_table("audit_log")
    op.drop_table("review_cycles")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
