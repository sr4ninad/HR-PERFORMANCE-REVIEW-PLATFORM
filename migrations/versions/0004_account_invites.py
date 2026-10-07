"""Account invites: a link can now be an invite (new account sets its first
password) or a reset. Existing links are backfilled as resets.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "password_reset_tokens",
        sa.Column("purpose", sa.String(length=16), server_default="reset", nullable=False),
    )


def downgrade() -> None:
    with op.batch_alter_table("password_reset_tokens") as batch_op:
        batch_op.drop_column("purpose")
