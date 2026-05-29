"""add card_brand and card_last4 to orders

Revision ID: b7e4a1c0d823
Revises: a3f1d2e9b047
Create Date: 2026-05-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7e4a1c0d823"
down_revision: Union[str, None] = "a3f1d2e9b047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("card_brand", sa.String(50), nullable=False, server_default=""))
    op.add_column("orders", sa.Column("card_last4", sa.String(4), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("orders", "card_last4")
    op.drop_column("orders", "card_brand")
