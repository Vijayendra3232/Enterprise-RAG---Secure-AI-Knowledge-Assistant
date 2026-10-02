"""001_initial_schema

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-09-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Tenants table
    op.create_table(
        'tenants',
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('tenant_id')
    )
    op.create_index(op.f('ix_tenants_tenant_id'), 'tenants', ['tenant_id'], unique=False)

    # 2. Users table
    op.create_table(
        'users',
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=64), nullable=False, server_default='VIEWER'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('groups', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
        sa.UniqueConstraint('tenant_id', 'email', name='uq_tenant_user_email')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=False)
    op.create_index(op.f('ix_users_tenant_id'), 'users', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_users_user_id'), 'users', ['user_id'], unique=False)

    # 3. Documents table
    op.create_table(
        'documents',
        sa.Column('document_id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False, server_default='upload'),
        sa.Column('source_document_id', sa.String(length=255), nullable=True),
        sa.Column('mime_type', sa.String(length=128), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('access_level', sa.String(length=32), nullable=False, server_default='PRIVATE'),
        sa.Column('permission_status', sa.String(length=32), nullable=False, server_default='KNOWN'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('storage_path', sa.String(length=1024), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.user_id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('document_id'),
        sa.UniqueConstraint('tenant_id', 'source', 'source_document_id', name='uq_tenant_source_doc')
    )
    op.create_index(op.f('ix_documents_content_hash'), 'documents', ['content_hash'], unique=False)
    op.create_index(op.f('ix_documents_document_id'), 'documents', ['document_id'], unique=False)
    op.create_index(op.f('ix_documents_owner_id'), 'documents', ['owner_id'], unique=False)
    op.create_index(op.f('ix_documents_source_document_id'), 'documents', ['source_document_id'], unique=False)
    op.create_index(op.f('ix_documents_status'), 'documents', ['status'], unique=False)
    op.create_index(op.f('ix_documents_tenant_id'), 'documents', ['tenant_id'], unique=False)

    # 4. Document Permissions table
    op.create_table(
        'document_permissions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('document_id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=64), nullable=True),
        sa.Column('user_id', sa.String(length=64), nullable=True),
        sa.Column('permission', sa.String(length=64), nullable=False, server_default='DOCUMENT_READ'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_permissions_document_id'), 'document_permissions', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_permissions_role'), 'document_permissions', ['role'], unique=False)
    op.create_index(op.f('ix_document_permissions_tenant_id'), 'document_permissions', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_document_permissions_user_id'), 'document_permissions', ['user_id'], unique=False)

    # 5. Document Chunks table
    op.create_table(
        'document_chunks',
        sa.Column('chunk_id', sa.String(length=64), nullable=False),
        sa.Column('document_id', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('metadata_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('chunk_id')
    )
    op.create_index(op.f('ix_document_chunks_chunk_id'), 'document_chunks', ['chunk_id'], unique=False)
    op.create_index(op.f('ix_document_chunks_content_hash'), 'document_chunks', ['content_hash'], unique=False)
    op.create_index(op.f('ix_document_chunks_document_id'), 'document_chunks', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_chunks_tenant_id'), 'document_chunks', ['tenant_id'], unique=False)

    # 6. Audit Events table
    op.create_table(
        'audit_events',
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=32), nullable=False),
        sa.Column('document_id', sa.String(length=64), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('details_json', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index(op.f('ix_audit_events_document_id'), 'audit_events', ['document_id'], unique=False)
    op.create_index(op.f('ix_audit_events_event_id'), 'audit_events', ['event_id'], unique=False)
    op.create_index(op.f('ix_audit_events_event_type'), 'audit_events', ['event_id'], unique=False)
    op.create_index(op.f('ix_audit_events_request_id'), 'audit_events', ['request_id'], unique=False)
    op.create_index(op.f('ix_audit_events_tenant_id'), 'audit_events', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_audit_events_timestamp'), 'audit_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_audit_events_user_id'), 'audit_events', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('document_chunks')
    op.drop_table('document_permissions')
    op.drop_table('documents')
    op.drop_table('users')
    op.drop_table('tenants')
