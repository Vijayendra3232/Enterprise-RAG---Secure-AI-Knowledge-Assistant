"""
audit.py — Security audit event database model for enterprise compliance and tamper-evident history.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.storage.database import Base


class AuditEvent(Base):
    """
    Append-only security audit log record.
    Preserves audit trail without storing confidential document bodies or credentials.
    """
    __tablename__ = "audit_events"

    event_id = Column(String(64), primary_key=True, index=True)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(String(64), nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    action = Column(String(64), nullable=False)
    result = Column(String(32), nullable=False)  # "success", "failure", "denied"
    document_id = Column(String(64), nullable=True, index=True)
    request_id = Column(String(64), nullable=True, index=True)
    details_json = Column(JSON, default=dict, nullable=False)

    # Relationships
    tenant = relationship("Tenant", back_populates="audit_events")

    def __repr__(self) -> str:
        return (
            f"<AuditEvent(event_id='{self.event_id}', event_type='{self.event_type}', "
            f"user_id='{self.user_id}', result='{self.result}')>"
        )
