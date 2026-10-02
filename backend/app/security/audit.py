"""
audit.py — Structured security audit logging for enterprise compliance and forensic tracing.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class AuditRecord(BaseModel):
    """
    Structured security audit record.
    Never contains passwords, JWT secrets, tokens, or raw confidential document bodies.
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_type: str
    tenant_id: str
    user_id: str
    action: str
    result: str  # "success", "failure", "denied"
    document_id: Optional[str] = None
    request_id: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class AuditLogger:
    """
    Singleton audit logger that outputs structured JSON logs and stores
    recent audit history for security verification testing.
    """
    def __init__(self, max_history: int = 1000):
        self._history: List[AuditRecord] = []
        self._max_history = max_history

    def log(self, record: AuditRecord) -> None:
        # Append to in-memory audit trace
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        # Output JSON structured log line
        log_json = record.model_dump_json()
        print(f"[AUDIT] {log_json}")

        # Persist to database if available
        try:
            from app.storage.database import SessionLocal
            from app.storage.repositories.audit_repository import SQLAuditRepository
            db = SessionLocal()
            try:
                repo = SQLAuditRepository(db)
                repo.record_event(record)
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
        except Exception:
            pass

    def get_events(
        self,
        event_type: Optional[str] = None,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None
    ) -> List[AuditRecord]:
        """Query audit history for security assertions."""
        events = self._history
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if user_id:
            events = [e for e in events if e.user_id == user_id]
        if tenant_id:
            events = [e for e in events if e.tenant_id == tenant_id]
        return events

    def clear(self) -> None:
        """Clear audit history (used in unit test setups)."""
        self._history.clear()


# Global singleton instance
audit_logger = AuditLogger()


def log_security_event(
    event_type: str,
    tenant_id: str,
    user_id: str,
    action: str,
    result: str,
    details: Optional[Dict[str, Any]] = None,
    document_id: Optional[str] = None,
    request_id: Optional[str] = None
) -> AuditRecord:
    """
    Convenience wrapper to log a structured security audit event.
    """
    record = AuditRecord(
        event_type=event_type,
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        result=result,
        document_id=document_id,
        request_id=request_id,
        details=details or {}
    )
    audit_logger.log(record)
    return record
