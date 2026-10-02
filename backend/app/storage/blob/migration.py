"""
migration.py — Resumable Local-to-S3 Storage Migration Service with Explicit State Machine.
PostgreSQL is the authoritative source for migration state, tracking state transitions,
authoritative SHA-256 verification, and orphan candidate lifecycle.
"""

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from app.storage.blob.base import DocumentStorageInterface
from app.storage.blob.errors import (
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
)
from app.storage.models.document import Document as DBDocument
from app.storage.repositories.document_repository import SQLDocumentRepository
from app.storage.repositories.migration_repository import SQLStorageMigrationRepository
from app.security.audit import log_security_event

logger = logging.getLogger(__name__)


class MigrationState(str, Enum):
    LOCAL_ONLY = "LOCAL_ONLY"
    UPLOADING = "UPLOADING"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    S3_VERIFIED = "S3_VERIFIED"
    INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
    DB_COMMITTED = "DB_COMMITTED"
    DB_COMMIT_FAILED = "DB_COMMIT_FAILED"
    MIGRATED = "MIGRATED"
    ALREADY_MIGRATED = "ALREADY_MIGRATED"
    WOULD_MIGRATE = "WOULD_MIGRATE"


@dataclass
class MigrationResult:
    document_id: str
    tenant_id: str
    source_path: str
    target_path: Optional[str]
    status: MigrationState
    content_hash: str
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


class StorageMigrationService:
    """
    Production-grade migration service for transitioning document blobs from local storage to S3.
    Operates with explicit state transitions persisted in PostgreSQL, SHA-256 verification, and orphan tracking.
    """

    def __init__(
        self,
        db: Session,
        target_storage: DocumentStorageInterface,
        local_storage: Optional[DocumentStorageInterface] = None,
    ):
        self.db = db
        self.target_storage = target_storage
        self.local_storage = local_storage
        self.doc_repo = SQLDocumentRepository(db)
        self.migration_repo = SQLStorageMigrationRepository(db)

    def get_orphan_candidates(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return list of S3 objects created where DB commit failed, queried authoritatively from PostgreSQL.
        """
        records = self.migration_repo.list_orphan_candidates(tenant_id=tenant_id)
        return [
            {
                "migration_id": r.id,
                "document_id": r.document_id,
                "tenant_id": r.tenant_id,
                "s3_path": f"s3://{r.target_bucket}/{r.target_key}" if r.target_bucket and r.target_key else r.target_key,
                "target_bucket": r.target_bucket,
                "target_key": r.target_key,
                "content_hash": r.content_hash,
                "status": r.status,
                "last_error": r.last_error,
                "reason": "db_commit_failed",
            }
            for r in records
        ]

    def migrate_document(
        self,
        document_id: str,
        tenant_id: str,
        dry_run: bool = False,
    ) -> MigrationResult:
        """
        Migrate a single document from local filesystem storage to S3 storage.
        All lifecycle states are persisted authoritatively in PostgreSQL.
        """
        db_doc = self.doc_repo.get_by_id(document_id, tenant_id=tenant_id)
        if not db_doc:
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path="",
                target_path=None,
                status=MigrationState.UPLOAD_FAILED,
                content_hash="",
                error=f"Document '{document_id}' not found in database for tenant '{tenant_id}'.",
            )

        source_path = db_doc.storage_path or ""
        content_hash = db_doc.content_hash
        version = db_doc.version or 1

        # ── 1. Check if already migrated to S3 ──────────────────────────────
        if source_path.startswith("s3://"):
            try:
                # Verify existing S3 object matches authoritative DB hash
                _ = self.target_storage.download_verified(source_path, expected_hash=content_hash)
                
                # Record in PostgreSQL migration state
                if not dry_run:
                    mig = self.migration_repo.get_or_create(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        document_version=version,
                        content_hash=content_hash,
                        source_path=source_path,
                        source_storage_type="s3",
                        target_storage_type="s3",
                    )
                    self.migration_repo.update_status(
                        mig.id,
                        tenant_id=tenant_id,
                        status=MigrationState.ALREADY_MIGRATED.value,
                        completed_at=datetime.now(timezone.utc),
                    )
                    self.db.commit()

                return MigrationResult(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    source_path=source_path,
                    target_path=source_path,
                    status=MigrationState.ALREADY_MIGRATED,
                    content_hash=content_hash,
                )
            except StorageIntegrityError as exc:
                return MigrationResult(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    source_path=source_path,
                    target_path=source_path,
                    status=MigrationState.INTEGRITY_MISMATCH,
                    content_hash=content_hash,
                    error=f"Existing S3 object failed integrity check: {exc}",
                )
            except Exception as exc:
                return MigrationResult(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    source_path=source_path,
                    target_path=source_path,
                    status=MigrationState.UPLOAD_FAILED,
                    content_hash=content_hash,
                    error=f"Failed to access existing S3 object: {exc}",
                )

        # ── 2. Validate Local Source File ───────────────────────────────────
        if not source_path or not os.path.exists(source_path):
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=None,
                status=MigrationState.UPLOAD_FAILED,
                content_hash=content_hash,
                error=f"Local source file does not exist at '{source_path}'.",
            )

        try:
            with open(source_path, "rb") as fh:
                local_content = fh.read()
        except Exception as exc:
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=None,
                status=MigrationState.UPLOAD_FAILED,
                content_hash=content_hash,
                error=f"Failed to read local source file: {exc}",
            )

        # Authoritative local checksum check
        local_hash = hashlib.sha256(local_content).hexdigest()
        if local_hash != content_hash:
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=None,
                status=MigrationState.INTEGRITY_MISMATCH,
                content_hash=content_hash,
                error=f"Local content hash '{local_hash}' does not match authoritative DB hash '{content_hash}'.",
            )

        if dry_run:
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=f"s3://[target-bucket]/tenants/{tenant_id}/documents/{document_id}/versions/{version}/{content_hash}",
                status=MigrationState.WOULD_MIGRATE,
                content_hash=content_hash,
            )

        # ── 3. Initialize / Resume PostgreSQL Migration State ──────────────
        mig = self.migration_repo.get_or_create(
            tenant_id=tenant_id,
            document_id=document_id,
            document_version=version,
            content_hash=content_hash,
            source_path=source_path,
            source_storage_type="local",
            target_storage_type="s3",
        )
        self.migration_repo.increment_attempt(mig.id, tenant_id=tenant_id)
        self.migration_repo.update_status(
            mig.id,
            tenant_id=tenant_id,
            status=MigrationState.UPLOADING.value,
        )
        self.db.commit()

        # ── 4. Upload to S3 (SSE-KMS CMK) ───────────────────────────────────
        target_s3_path: Optional[str] = None
        try:
            target_s3_path = self.target_storage.save(
                content=local_content,
                filename=db_doc.filename,
                tenant_id=tenant_id,
                document_id=document_id,
                version=version,
                expected_hash=content_hash,
            )
        except Exception as exc:
            logger.error(f"[Migration] S3 upload failed for doc {document_id}: {exc}")
            self.migration_repo.update_status(
                mig.id,
                tenant_id=tenant_id,
                status=MigrationState.UPLOAD_FAILED.value,
                last_error=str(exc),
            )
            self.db.commit()
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=None,
                status=MigrationState.UPLOAD_FAILED,
                content_hash=content_hash,
                error=str(exc),
            )

        # ── 5. Verify Uploaded S3 Content Integrity ─────────────────────────
        try:
            _ = self.target_storage.download_verified(target_s3_path, expected_hash=content_hash)
            now = datetime.now(timezone.utc)
            self.migration_repo.update_status(
                mig.id,
                tenant_id=tenant_id,
                status=MigrationState.S3_VERIFIED.value,
                target_key=target_s3_path,
                verified_at=now,
            )
            self.db.commit()
        except StorageIntegrityError as exc:
            logger.error(f"[Migration] Post-upload integrity check failed on {target_s3_path}: {exc}")
            self.migration_repo.update_status(
                mig.id,
                tenant_id=tenant_id,
                status=MigrationState.INTEGRITY_MISMATCH.value,
                target_key=target_s3_path,
                last_error=str(exc),
            )
            self.db.commit()
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=target_s3_path,
                status=MigrationState.INTEGRITY_MISMATCH,
                content_hash=content_hash,
                error=str(exc),
            )
        except Exception as exc:
            self.migration_repo.update_status(
                mig.id,
                tenant_id=tenant_id,
                status=MigrationState.UPLOAD_FAILED.value,
                target_key=target_s3_path,
                last_error=str(exc),
            )
            self.db.commit()
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=target_s3_path,
                status=MigrationState.UPLOAD_FAILED,
                content_hash=content_hash,
                error=f"Failed verifying uploaded S3 object: {exc}",
            )

        # ── 6. Transactionally Update Authoritative Document Record ─────────
        try:
            db_doc.storage_path = target_s3_path
            now = datetime.now(timezone.utc)
            self.migration_repo.update_status(
                mig.id,
                tenant_id=tenant_id,
                status=MigrationState.DB_COMMITTED.value,
                committed_at=now,
            )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            # Authoritatively record orphan candidate in PostgreSQL
            try:
                self.migration_repo.update_status(
                    mig.id,
                    tenant_id=tenant_id,
                    status=MigrationState.DB_COMMIT_FAILED.value,
                    is_orphan_candidate=True,
                    target_key=target_s3_path,
                    last_error=f"DB commit failed: {exc}",
                )
                self.db.commit()
            except Exception:
                pass
            logger.error(f"[Migration] DB commit failed after S3 upload for {document_id}: {exc}")
            return MigrationResult(
                document_id=document_id,
                tenant_id=tenant_id,
                source_path=source_path,
                target_path=target_s3_path,
                status=MigrationState.DB_COMMIT_FAILED,
                content_hash=content_hash,
                error=f"DB commit failed after S3 upload: {exc}",
            )

        # ── 7. Mark MIGRATED State in PostgreSQL ───────────────────────────
        now = datetime.now(timezone.utc)
        self.migration_repo.update_status(
            mig.id,
            tenant_id=tenant_id,
            status=MigrationState.MIGRATED.value,
            completed_at=now,
            is_orphan_candidate=False,
        )
        self.db.commit()

        # ── 8. Log Security Event & Return Success ──────────────────────────
        log_security_event(
            event_type="STORAGE_MIGRATION",
            tenant_id=tenant_id,
            user_id="system_migration",
            action="migrate_to_s3",
            result="success",
            document_id=document_id,
            details={
                "migration_id": mig.id,
                "source_path": source_path,
                "target_path": target_s3_path,
                "content_hash": content_hash,
            },
        )

        return MigrationResult(
            document_id=document_id,
            tenant_id=tenant_id,
            source_path=source_path,
            target_path=target_s3_path,
            status=MigrationState.MIGRATED,
            content_hash=content_hash,
            details={"migration_id": mig.id},
        )

    def migrate_tenant(
        self,
        tenant_id: str,
        dry_run: bool = False,
    ) -> List[MigrationResult]:
        """Migrate all documents belonging to a specific tenant."""
        docs = self.doc_repo.list_by_tenant(tenant_id=tenant_id, limit=10000)
        results = []
        for doc in docs:
            result = self.migrate_document(
                document_id=doc.document_id,
                tenant_id=tenant_id,
                dry_run=dry_run,
            )
            results.append(result)
        return results

    def migrate_all(self, dry_run: bool = False) -> List[MigrationResult]:
        """Migrate all documents across all tenants."""
        docs = self.db.query(DBDocument).all()
        results = []
        for doc in docs:
            result = self.migrate_document(
                document_id=doc.document_id,
                tenant_id=doc.tenant_id,
                dry_run=dry_run,
            )
            results.append(result)
        return results
