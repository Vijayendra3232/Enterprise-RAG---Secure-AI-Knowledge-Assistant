"""
document.py — Document metadata database model tracking ingestion state, source, hashing, and access levels.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.storage.database import Base


class Document(Base):
    """
    Authoritative database record representing an ingested document and its lifecycle state.
    """
    __tablename__ = "documents"

    document_id = Column(String(64), primary_key=True, index=True)
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id = Column(
        String(64),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    filename = Column(String(255), nullable=False)
    source = Column(String(64), default="upload", nullable=False)
    source_document_id = Column(String(255), nullable=True, index=True)
    mime_type = Column(String(128), nullable=True)
    size_bytes = Column(Integer, default=0, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    access_level = Column(String(32), default="PRIVATE", nullable=False)
    permission_status = Column(String(32), default="KNOWN", nullable=False)
    status = Column(String(32), default="PENDING", nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    version = Column(Integer, default=1, nullable=False)
    storage_path = Column(String(1024), nullable=True)
    metadata_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    indexed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source",
            "source_document_id",
            name="uq_tenant_source_doc",
        ),
    )

    # Relationships
    tenant = relationship("Tenant", back_populates="documents")
    owner = relationship("User", back_populates="uploaded_documents")
    permissions = relationship(
        "DocumentPermission",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    chunks = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Document(document_id='{self.document_id}', tenant_id='{self.tenant_id}', "
            f"filename='{self.filename}', status='{self.status}', version={self.version})>"
        )
