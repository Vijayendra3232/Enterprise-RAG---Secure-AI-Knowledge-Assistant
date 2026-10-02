"""004_cloud_connectors_and_sync_cursor

Revision ID: 004_cloud_connectors_and_sync_cursor
Revises: 003_async_tasks_and_outbox
Create Date: 2026-09-09 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004_cloud_connectors_and_sync_cursor'
down_revision = '003_async_tasks_and_outbox'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add sync_cursor and sync_lock_at to connector_configs
    with op.batch_alter_table('connector_configs') as batch_op:
        batch_op.add_column(sa.Column('sync_cursor', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('sync_lock_at', sa.DateTime(timezone=True), nullable=True))

    # 2. Add cursor_before and cursor_after to sync_runs
    with op.batch_alter_table('sync_runs') as batch_op:
        batch_op.add_column(sa.Column('cursor_before', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('cursor_after', sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sync_runs') as batch_op:
        batch_op.drop_column('cursor_after')
        batch_op.drop_column('cursor_before')

    with op.batch_alter_table('connector_configs') as batch_op:
        batch_op.drop_column('sync_lock_at')
        batch_op.drop_column('sync_cursor')
