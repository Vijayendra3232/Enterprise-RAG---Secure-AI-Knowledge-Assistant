"""
migration.py — SQLAlchemy database model for durable local-to-cloud storage migrations.
Authoritative state in PostgreSQL tracking migration state machine, verification timestamps,
orphan candidates, and tenant isolation.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from app.storage.database import Base


class DocumentStorageMigration(Base):
    """
    Durable storage migration record in PostgreSQL.
    Authoritative state for tracking document transitions from local storage to S3.
    """
    __tablename__ = "document_storage_migrations"

    id = Column(String(64), primary_key=True, index=True)
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(String(128), nullable=False, index=True)
    document_version = Column(Integer, nullable=False, default=1)
    
    source_storage_type = Column(String(32), nullable=False, default="local")
    source_path = Column(Text, nullable=False)
    target_storage_type = Column(String(32), nullable=False, default="s3")
    target_bucket = Column(String(128), nullable=True)
    target_key = Column(Text, nullable=True)
    
    content_hash = Column(String(64), nullable=False, index=True)
    
    # State Machine: LOCAL_ONLY -> UPLOADING -> S3_VERIFIED -> DB_COMMITTED -> MIGRATED
    # Failure states: UPLOAD_FAILED, INTEGRITY_MISMATCH, DB_COMMIT_FAILED
    status = Column(String(32), nullable=False, default="LOCAL_ONLY", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    is_orphan_candidate = Column(Boolean, nullable=False, default=False, index=True)
    
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    committed_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "document_version",
            "content_hash",
            name="uq_tenant_doc_version_hash_migration",
        ),
        Index("ix_migrations_tenant_status", "tenant_id", "status"),
        Index("ix_migrations_orphan_candidate", "is_orphan_candidate"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "source_storage_type": self.source_storage_type,
            "source_path": self.source_path,
            "target_storage_type": self.target_storage_type,
            "target_bucket": self.target_bucket,
            "target_key": self.target_key,
            "content_hash": self.content_hash,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "is_orphan_candidate": self.is_orphan_candidate,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
