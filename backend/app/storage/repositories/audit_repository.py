"""
audit_repository.py — Security audit repository interface and SQLAlchemy implementation.
Persists security events into append-only database records for compliance.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional, List, Any
from sqlalchemy.orm import Session

from app.storage.models.audit import AuditEvent


class AuditRepositoryInterface(ABC):
    @abstractmethod
    def record_event(self, record: Any) -> AuditEvent:
        pass

    @abstractmethod
    def query_events(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        document_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditEvent]:
        pass

    @abstractmethod
    def count(self, tenant_id: Optional[str] = None) -> int:
        pass


class SQLAuditRepository(AuditRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def record_event(self, record: Any) -> AuditEvent:
        # Accepts either AuditRecord Pydantic object or dict or AuditEvent model
        if isinstance(record, AuditEvent):
            event = record
        else:
            # Handle Pydantic model or dict
            data = record.model_dump() if hasattr(record, "model_dump") else dict(record)
            ts = data.get("timestamp")
            if isinstance(ts, str):
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    ts_dt = datetime.now(timezone.utc)
            elif isinstance(ts, datetime):
                ts_dt = ts
            else:
                ts_dt = datetime.now(timezone.utc)

            event = AuditEvent(
                event_id=data.get("event_id"),
                timestamp=ts_dt,
                tenant_id=data.get("tenant_id"),
                user_id=data.get("user_id"),
                event_type=data.get("event_type"),
                action=data.get("action"),
                result=data.get("result"),
                document_id=data.get("document_id"),
                request_id=data.get("request_id"),
                details_json=data.get("details", {}),
            )

        self.db.add(event)
        self.db.flush()
        return event

    def query_events(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        document_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditEvent]:
        query = self.db.query(AuditEvent)
        if tenant_id:
            query = query.filter(AuditEvent.tenant_id == tenant_id)
        if user_id:
            query = query.filter(AuditEvent.user_id == user_id)
        if event_type:
            query = query.filter(AuditEvent.event_type == event_type)
        if document_id:
            query = query.filter(AuditEvent.document_id == document_id)
        return query.order_by(AuditEvent.timestamp.desc()).offset(offset).limit(limit).all()

    def count(self, tenant_id: Optional[str] = None) -> int:
        query = self.db.query(AuditEvent)
        if tenant_id:
            query = query.filter(AuditEvent.tenant_id == tenant_id)
        return query.count()
