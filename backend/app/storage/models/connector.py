"""
connector.py — Connector configuration database model storing tenant-scoped connector definitions and encrypted credentials.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.storage.database import Base


class ConnectorConfig(Base):
    """
    Authoritative database record storing connector configuration and encrypted secrets.
    """
    __tablename__ = "connector_configs"

    id = Column(String(64), primary_key=True, index=True)
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    connector_type = Column(String(64), nullable=False, index=True)  # e.g., "local", "google_drive"
    status = Column(String(32), default="ACTIVE", nullable=False, index=True)  # ACTIVE, DISABLED, ERROR, SYNCING
    encrypted_config = Column(Text, nullable=False)  # Ciphertext containing secrets/path/params
    sync_cursor = Column(Text, nullable=True)  # Provider continuation / change token
    sync_lock_at = Column(DateTime(timezone=True), nullable=True)  # Timestamp when current sync lock was acquired
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
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
        UniqueConstraint("tenant_id", "name", name="uq_tenant_connector_name"),
    )

    # Relationships
    tenant = relationship("Tenant")
    sync_runs = relationship(
        "SyncRun",
        back_populates="connector",
        cascade="all, delete-orphan",
        order_by="desc(SyncRun.started_at)",
    )

    def __repr__(self) -> str:
        return (
            f"<ConnectorConfig(id='{self.id}', tenant_id='{self.tenant_id}', "
            f"name='{self.name}', type='{self.connector_type}', status='{self.status}')>"
        )
