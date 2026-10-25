"""subscription period, usage idempotency per type

Revision ID: 5f3332f9a3c2
Revises: face5ba231cf
Create Date: 2026-10-25 01:40:55.678173

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5f3332f9a3c2'
down_revision: Union[str, Sequence[str], None] = 'face5ba231cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint('uq_tenant_idem', 'usage_events', type_='unique')
    op.create_unique_constraint('uq_tenant_type_idem', 'usage_events', ['tenant_id', 'type', 'idempotency_key'])


def downgrade() -> None:
    op.drop_constraint('uq_tenant_type_idem', 'usage_events', type_='unique')
    op.create_unique_constraint('uq_tenant_idem', 'usage_events', ['tenant_id', 'idempotency_key'])
    op.drop_column('subscriptions', 'current_period_end')
    op.drop_column('subscriptions', 'current_period_start')
