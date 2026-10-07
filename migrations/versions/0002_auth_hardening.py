"""Session versioning, DB-backed login throttling, password reset tokens,
and a database-enforced "only one active cycle" rule.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_ONLY = sa.text("status = 'active'")


def upgrade() -> None:
    # server_default so existing rows get 0 -- SQLite can't add a NOT NULL
    # column without one.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("session_version", sa.Integer(), server_default="0", nullable=False))

    # Before adding the unique index, repair any database that already has
    # more than one active cycle (the old app enforced this only in code):
    # keep the newest active cycle, close the rest.
    op.execute(
        "UPDATE review_cycles SET status = 'closed' "
        "WHERE status = 'active' "
        "AND id <> (SELECT MAX(id) FROM review_cycles WHERE status = 'active')"
    )
    op.create_index(
        "uq_one_active_cycle",
        "review_cycles",
        ["status"],
        unique=True,
        sqlite_where=_ACTIVE_ONLY,
        postgresql_where=_ACTIVE_ONLY,
    )

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_attempts_key_time", "login_attempts", ["key", "attempted_at"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["issued_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_login_attempts_key_time", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_index("uq_one_active_cycle", table_name="review_cycles")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("session_version")
