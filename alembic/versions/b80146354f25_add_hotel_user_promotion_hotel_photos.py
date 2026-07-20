"""add hotel_user, promotion, hotel photos

Revision ID: b80146354f25
Revises: 8e640ac69e2c
Create Date: 2026-07-20 00:35:39.984226

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b80146354f25'
down_revision: Union[str, None] = '8e640ac69e2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also proposed re-creating fk_booking_reference_payment_id
    # here -- the same use_alter=True false positive documented in
    # 8e640ac69e2c (that FK is already created inline by the initial
    # migration's create_table() call). Stripped.
    op.create_table('hotel_user',
    sa.Column('hotel_user_id', sa.UUID(), nullable=False),
    sa.Column('hotel_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('owner', 'manager', name='hotel_user_role'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['hotel_id'], ['hotel.hotel_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('hotel_user_id'),
    sa.UniqueConstraint('email')
    )
    op.create_index(op.f('ix_hotel_user_hotel_id'), 'hotel_user', ['hotel_id'], unique=False)
    op.create_table('promotion',
    sa.Column('promotion_id', sa.UUID(), nullable=False),
    sa.Column('hotel_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('discount_percentage', sa.Float(), nullable=False),
    sa.Column('starts_on', sa.Date(), nullable=False),
    sa.Column('ends_on', sa.Date(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['hotel_id'], ['hotel.hotel_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('promotion_id')
    )
    op.create_index(op.f('ix_promotion_hotel_id'), 'promotion', ['hotel_id'], unique=False)
    # server_default only so ADD COLUMN succeeds against any hotel rows that
    # already exist; dropped right after so future inserts rely on the ORM
    # model's own Python-side default instead of a DB-level one.
    op.add_column('hotel', sa.Column('photos', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'))
    op.alter_column('hotel', 'photos', server_default=None)


def downgrade() -> None:
    op.drop_column('hotel', 'photos')
    op.drop_index(op.f('ix_promotion_hotel_id'), table_name='promotion')
    op.drop_table('promotion')
    op.drop_index(op.f('ix_hotel_user_hotel_id'), table_name='hotel_user')
    op.drop_table('hotel_user')
