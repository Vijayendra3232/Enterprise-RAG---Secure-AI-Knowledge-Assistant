"""
migration_repository.py — SQLAlchemy repository for authoritative DocumentStorageMigration entities.
Enforces tenant isolation, state machine tracking, and orphan candidate query semantics.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session

from app.storage.models.migration import DocumentStorageMigration


class SQLStorageMigrationRepository:
    """
    Authoritative storage migration repository in PostgreSQL.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_or_create(
        self,
        tenant_id: str,
        document_id: str,
        document_version: int,
        content_hash: str,
        source_path: str,
        source_storage_type: str = "local",
        target_storage_type: str = "s3",
    ) -> DocumentStorageMigration:
        """
        Idempotently get or create a migration tracking record for a document version.
        """
        record = (
            self.db.query(DocumentStorageMigration)
            .filter(
                DocumentStorageMigration.tenant_id == tenant_id,
                DocumentStorageMigration.document_id == document_id,
                DocumentStorageMigration.document_version == document_version,
                DocumentStorageMigration.content_hash == content_hash,
            )
            .first()
        )
        if not record:
            record = DocumentStorageMigration(
                id=f"mig_{uuid.uuid4().hex[:16]}",
                tenant_id=tenant_id,
                document_id=document_id,
                document_version=document_version,
                source_storage_type=source_storage_type,
                source_path=source_path,
                target_storage_type=target_storage_type,
                content_hash=content_hash,
                status="LOCAL_ONLY",
                attempt_count=0,
            )
            self.db.add(record)
            self.db.flush()
        return record

    def get_by_id(self, migration_id: str, tenant_id: str) -> Optional[DocumentStorageMigration]:
        return (
            self.db.query(DocumentStorageMigration)
            .filter(
                DocumentStorageMigration.id == migration_id,
                DocumentStorageMigration.tenant_id == tenant_id,
            )
            .first()
        )

    def get_by_document(self, document_id: str, tenant_id: str) -> Optional[DocumentStorageMigration]:
        return (
            self.db.query(DocumentStorageMigration)
            .filter(
                DocumentStorageMigration.document_id == document_id,
                DocumentStorageMigration.tenant_id == tenant_id,
            )
            .order_by(DocumentStorageMigration.created_at.desc())
            .first()
        )

    def update_status(
        self,
        migration_id: str,
        tenant_id: str,
        status: str,
        last_error: Optional[str] = None,
        is_orphan_candidate: Optional[bool] = None,
        target_bucket: Optional[str] = None,
        target_key: Optional[str] = None,
        verified_at: Optional[datetime] = None,
        committed_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> Optional[DocumentStorageMigration]:
        record = self.get_by_id(migration_id, tenant_id)
        if not record:
            return None

        record.status = status
        record.updated_at = datetime.now(timezone.utc)

        if last_error is not None:
            record.last_error = last_error
        if is_orphan_candidate is not None:
            record.is_orphan_candidate = is_orphan_candidate
        if target_bucket is not None:
            record.target_bucket = target_bucket
        if target_key is not None:
            record.target_key = target_key
        if verified_at is not None:
            record.verified_at = verified_at
        if committed_at is not None:
            record.committed_at = committed_at
        if completed_at is not None:
            record.completed_at = completed_at

        self.db.flush()
        return record

    def increment_attempt(self, migration_id: str, tenant_id: str) -> Optional[DocumentStorageMigration]:
        record = self.get_by_id(migration_id, tenant_id)
        if not record:
            return None
        record.attempt_count += 1
        record.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return record

    def list_by_tenant(
        self,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[DocumentStorageMigration]:
        return (
            self.db.query(DocumentStorageMigration)
            .filter(DocumentStorageMigration.tenant_id == tenant_id)
            .order_by(DocumentStorageMigration.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def list_orphan_candidates(
        self,
        tenant_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[DocumentStorageMigration]:
        query = self.db.query(DocumentStorageMigration).filter(
            DocumentStorageMigration.is_orphan_candidate.is_(True)
        )
        if tenant_id:
            query = query.filter(DocumentStorageMigration.tenant_id == tenant_id)
        return query.order_by(DocumentStorageMigration.created_at.desc()).offset(offset).limit(limit).all()

    def list_failed(
        self,
        tenant_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[DocumentStorageMigration]:
        query = self.db.query(DocumentStorageMigration).filter(
            DocumentStorageMigration.status.in_(["UPLOAD_FAILED", "INTEGRITY_MISMATCH", "DB_COMMIT_FAILED"])
        )
        if tenant_id:
            query = query.filter(DocumentStorageMigration.tenant_id == tenant_id)
        return query.order_by(DocumentStorageMigration.created_at.desc()).offset(offset).limit(limit).all()
