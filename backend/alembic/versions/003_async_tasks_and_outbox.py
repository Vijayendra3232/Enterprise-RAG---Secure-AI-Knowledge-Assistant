"""003_async_tasks_and_outbox

Revision ID: 003_async_tasks_and_outbox
Revises: 002_connectors_and_sync
Create Date: 2026-09-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '003_async_tasks_and_outbox'
down_revision = '002_connectors_and_sync'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create tasks table
    op.create_table(
        'tasks',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('task_type', sa.String(length=64), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column('aggregate_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('worker_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_tenant_task_idempotency')
    )
    op.create_index(op.f('ix_tasks_id'), 'tasks', ['id'], unique=False)
    op.create_index(op.f('ix_tasks_tenant_id'), 'tasks', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tasks_task_type'), 'tasks', ['task_type'], unique=False)
    op.create_index(op.f('ix_tasks_idempotency_key'), 'tasks', ['idempotency_key'], unique=False)
    op.create_index(op.f('ix_tasks_aggregate_id'), 'tasks', ['aggregate_id'], unique=False)
    op.create_index(op.f('ix_tasks_status'), 'tasks', ['status'], unique=False)
    op.create_index(op.f('ix_tasks_available_at'), 'tasks', ['available_at'], unique=False)
    op.create_index('ix_tasks_tenant_status', 'tasks', ['tenant_id', 'status'], unique=False)
    op.create_index('ix_tasks_status_available', 'tasks', ['status', 'available_at'], unique=False)
    op.create_index('ix_tasks_tenant_type', 'tasks', ['tenant_id', 'task_type'], unique=False)

    # 2. Create outbox_events table
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('aggregate_id', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outbox_events_id'), 'outbox_events', ['id'], unique=False)
    op.create_index(op.f('ix_outbox_events_tenant_id'), 'outbox_events', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_outbox_events_event_type'), 'outbox_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_outbox_events_aggregate_id'), 'outbox_events', ['aggregate_id'], unique=False)
    op.create_index(op.f('ix_outbox_events_status'), 'outbox_events', ['status'], unique=False)
    op.create_index('ix_outbox_status_created', 'outbox_events', ['status', 'created_at'], unique=False)
    op.create_index('ix_outbox_tenant_status', 'outbox_events', ['tenant_id', 'status'], unique=False)


def downgrade() -> None:
    op.drop_table('outbox_events')
    op.drop_table('tasks')
