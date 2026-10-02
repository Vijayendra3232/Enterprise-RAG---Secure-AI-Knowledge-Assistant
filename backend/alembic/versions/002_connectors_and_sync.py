"""002_connectors_and_sync

Revision ID: 002_connectors_and_sync
Revises: 001_initial_schema
Create Date: 2026-09-07 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002_connectors_and_sync'
down_revision = '001_initial_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create connector_configs table
    op.create_table(
        'connector_configs',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('connector_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('encrypted_config', sa.Text(), nullable=False),
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_tenant_connector_name')
    )
    op.create_index(op.f('ix_connector_configs_id'), 'connector_configs', ['id'], unique=False)
    op.create_index(op.f('ix_connector_configs_tenant_id'), 'connector_configs', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_connector_configs_connector_type'), 'connector_configs', ['connector_type'], unique=False)
    op.create_index(op.f('ix_connector_configs_status'), 'connector_configs', ['status'], unique=False)

    # 2. Create sync_runs table
    op.create_table(
        'sync_runs',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('connector_id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('sync_mode', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='SYNCING'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('documents_seen', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('documents_added', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('documents_updated', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('documents_deleted', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('permissions_updated', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('documents_skipped', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('errors_json', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['connector_id'], ['connector_configs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sync_runs_id'), 'sync_runs', ['id'], unique=False)
    op.create_index(op.f('ix_sync_runs_connector_id'), 'sync_runs', ['connector_id'], unique=False)
    op.create_index(op.f('ix_sync_runs_tenant_id'), 'sync_runs', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_sync_runs_status'), 'sync_runs', ['status'], unique=False)

    # 3. Add group_id and effect columns to document_permissions
    with op.batch_alter_table('document_permissions') as batch_op:
        batch_op.add_column(sa.Column('group_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('effect', sa.String(length=16), nullable=False, server_default='ALLOW'))
        batch_op.create_index('ix_document_permissions_group_id', ['group_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('document_permissions') as batch_op:
        batch_op.drop_index('ix_document_permissions_group_id')
        batch_op.drop_column('effect')
        batch_op.drop_column('group_id')

    op.drop_index(op.f('ix_sync_runs_status'), table_name='sync_runs')
    op.drop_index(op.f('ix_sync_runs_tenant_id'), table_name='sync_runs')
    op.drop_index(op.f('ix_sync_runs_connector_id'), table_name='sync_runs')
    op.drop_index(op.f('ix_sync_runs_id'), table_name='sync_runs')
    op.drop_table('sync_runs')

    op.drop_index(op.f('ix_connector_configs_status'), table_name='connector_configs')
    op.drop_index(op.f('ix_connector_configs_connector_type'), table_name='connector_configs')
    op.drop_index(op.f('ix_connector_configs_tenant_id'), table_name='connector_configs')
    op.drop_index(op.f('ix_connector_configs_id'), table_name='connector_configs')
    op.drop_table('connector_configs')
