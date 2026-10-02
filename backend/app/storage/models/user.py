"""
user.py — User entity database model supporting tenant-scoped identity and role assignments.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import relationship

from app.storage.database import Base


class User(Base):
    """
    Authoritative persistent user account model.
    """
    __tablename__ = "users"

    user_id = Column(String(64), primary_key=True, index=True)
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(64), nullable=False, default="VIEWER")
    is_active = Column(Boolean, default=True, nullable=False)
    groups = Column(JSON, default=list, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_tenant_user_email"),
    )

    # Relationships
    tenant = relationship("Tenant", back_populates="users")
    uploaded_documents = relationship("Document", back_populates="owner")
    granted_permissions = relationship("DocumentPermission", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User(user_id='{self.user_id}', tenant_id='{self.tenant_id}', email='{self.email}', role='{self.role}')>"
