"""
sync_run.py — Sync run database model tracking synchronization execution, counts, metrics, and error logs.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship

from app.storage.database import Base


class SyncRun(Base):
    """
    Persistent audit log record of a connector sync execution.
    """
    __tablename__ = "sync_runs"

    id = Column(String(64), primary_key=True, index=True)
    connector_id = Column(
        String(64),
        ForeignKey("connector_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sync_mode = Column(String(32), nullable=False)  # "FULL", "INCREMENTAL"
    status = Column(String(32), default="SYNCING", nullable=False, index=True)  # "SYNCING", "SUCCESS", "ERROR", "PARTIAL"
    started_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Metrics
    cursor_before = Column(String(512), nullable=True)
    cursor_after = Column(String(512), nullable=True)
    documents_seen = Column(Integer, default=0, nullable=False)
    documents_added = Column(Integer, default=0, nullable=False)
    documents_updated = Column(Integer, default=0, nullable=False)
    documents_deleted = Column(Integer, default=0, nullable=False)
    permissions_updated = Column(Integer, default=0, nullable=False)
    documents_skipped = Column(Integer, default=0, nullable=False)
    errors_json = Column(JSON, default=list, nullable=False)

    # Relationships
    connector = relationship("ConnectorConfig", back_populates="sync_runs")
    tenant = relationship("Tenant")

    def __repr__(self) -> str:
        return (
            f"<SyncRun(id='{self.id}', connector_id='{self.connector_id}', "
            f"mode='{self.sync_mode}', status='{self.status}')>"
        )
