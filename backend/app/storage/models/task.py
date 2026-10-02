"""
task.py — SQLAlchemy database model for durable asynchronous tasks.
Includes tenant-scoped idempotency keys, execution lifecycle states,
bounded retry tracking, and sanitized payloads.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    Text,
    JSON,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from app.storage.database import Base


class Task(Base):
    """
    Durable asynchronous task record in PostgreSQL.
    Authoritative state for background and distributed worker execution.
    """
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True)
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_type = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=True, index=True)
    aggregate_id = Column(String(64), nullable=True, index=True)  # e.g. document_id, connector_id
    
    # Lifecycle: PENDING -> RUNNING -> SUCCEEDED / RETRYING / FAILED / CANCELLED
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    priority = Column(Integer, nullable=False, default=0)
    
    # Sanitized payload: contains only identifiers and operational params. ZERO plaintext secrets.
    payload = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=True)
    
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    
    # Scheduling & backoff
    available_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Diagnostic info (sanitized, zero secrets)
    last_error = Column(Text, nullable=True)
    worker_id = Column(String(64), nullable=True)
    
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

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tenant_task_idempotency"),
        Index("ix_tasks_tenant_status", "tenant_id", "status"),
        Index("ix_tasks_status_available", "status", "available_at"),
        Index("ix_tasks_tenant_type", "tenant_id", "task_type"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "task_type": self.task_type,
            "idempotency_key": self.idempotency_key,
            "aggregate_id": self.aggregate_id,
            "status": self.status,
            "priority": self.priority,
            "payload": self.payload,
            "result": self.result,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "failed_at": self.failed_at.isoformat() if self.failed_at else None,
            "last_error": self.last_error,
            "worker_id": self.worker_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
