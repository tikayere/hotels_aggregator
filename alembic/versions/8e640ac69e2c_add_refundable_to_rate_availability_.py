"""add refundable to rate_availability_index

Revision ID: 8e640ac69e2c
Revises: 8db02c2cd0af
Create Date: 2026-07-19 12:44:59.728902

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8e640ac69e2c'
down_revision: Union[str, None] = '8db02c2cd0af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also proposed re-creating fk_booking_reference_payment_id
    # here -- a false positive from how it diffs use_alter=True foreign keys
    # that are already created inline by the initial migration's
    # create_table() call (see 8db02c2cd0af). Stripped; applying it for real
    # would fail with "constraint already exists" against any database that's
    # already on head.
    op.add_column('rate_availability_index', sa.Column('refundable', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('rate_availability_index', 'refundable')
