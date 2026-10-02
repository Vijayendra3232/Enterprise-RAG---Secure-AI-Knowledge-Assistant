"""
outbox.py — SQLAlchemy database model for the Transactional Outbox pattern.
Guarantees reliable message publication from PostgreSQL transactions to the Task Queue.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    JSON,
    ForeignKey,
    Index,
)
from app.storage.database import Base


class OutboxEvent(Base):
    """
    Transactional Outbox event record.
    Written atomically in the same database transaction as the business entity and Task record.
    """
    __tablename__ = "outbox_events"

    id = Column(String(64), primary_key=True, index=True)
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String(64), nullable=False, index=True)
    aggregate_id = Column(String(64), nullable=False, index=True)
    
    # Sanitized payload
    payload = Column(JSON, nullable=False, default=dict)
    
    # Status: PENDING -> PUBLISHED / FAILED
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    published_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_outbox_status_created", "status", "created_at"),
        Index("ix_outbox_tenant_status", "tenant_id", "status"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "payload": self.payload,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }
