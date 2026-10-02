"""
tenant.py — Tenant metadata database model for strict multi-tenant isolation.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.orm import relationship

from app.storage.database import Base


class Tenant(Base):
    """
    Authoritative tenant entity defining organizational boundaries.
    """
    __tablename__ = "tenants"

    tenant_id = Column(String(64), primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
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

    # Relationships
    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="tenant", cascade="all, delete-orphan")
    audit_events = relationship("AuditEvent", back_populates="tenant", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Tenant(tenant_id='{self.tenant_id}', name='{self.name}', is_active={self.is_active})>"
