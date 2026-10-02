"""
permission.py — Document permissions database model storing granular role and user access grants.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.storage.database import Base


class DocumentPermission(Base):
    """
    Persistent document permission grant associating documents with roles or individual users.
    """
    __tablename__ = "document_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
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
    role = Column(String(64), nullable=True, index=True)
    user_id = Column(
        String(64),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    group_id = Column(String(64), nullable=True, index=True)
    permission = Column(String(64), default="DOCUMENT_READ", nullable=False)
    effect = Column(String(16), default="ALLOW", nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    document = relationship("Document", back_populates="permissions")
    user = relationship("User", back_populates="granted_permissions")

    def __repr__(self) -> str:
        return (
            f"<DocumentPermission(id={self.id}, document_id='{self.document_id}', "
            f"role='{self.role}', user_id='{self.user_id}', permission='{self.permission}')>"
        )
