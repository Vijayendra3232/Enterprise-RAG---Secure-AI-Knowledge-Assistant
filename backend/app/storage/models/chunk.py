"""
chunk.py — Document chunk database model persisting raw chunk text and metadata for derived index recovery.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.storage.database import Base


class DocumentChunk(Base):
    """
    Authoritative chunk entity.
    Allows complete reconstruction of derived vector and BM25 search indexes directly from PostgreSQL.
    """
    __tablename__ = "document_chunks"

    chunk_id = Column(String(64), primary_key=True, index=True)
    document_id = Column(
        String(64),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    content = Column(Text, nullable=False)
    metadata_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    document = relationship("Document", back_populates="chunks")

    def __repr__(self) -> str:
        return (
            f"<DocumentChunk(chunk_id='{self.chunk_id}', document_id='{self.document_id}', "
            f"chunk_index={self.chunk_index})>"
        )
