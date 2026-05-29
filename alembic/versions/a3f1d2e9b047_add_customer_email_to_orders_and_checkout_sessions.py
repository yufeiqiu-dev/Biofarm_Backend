"""add customer_email to orders and checkout_sessions

Revision ID: a3f1d2e9b047
Revises: c09c8c1b08ed
Create Date: 2026-05-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3f1d2e9b047"
down_revision: Union[str, None] = "c09c8c1b08ed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("customer_email", sa.String(255), nullable=False, server_default=""))
    op.add_column("checkout_sessions", sa.Column("customer_email", sa.String(255), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("orders", "customer_email")
    op.drop_column("checkout_sessions", "customer_email")
