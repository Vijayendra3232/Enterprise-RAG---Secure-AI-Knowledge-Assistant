"""
service.py — Synchronization orchestrator executing full and incremental connector sync runs.
Reuses the authoritative ingestion pipeline, blob storage, and derived index adapters while
strictly preserving pre-retrieval authorization, concurrency locks, transactional cursor advancement,
and zero-re-embedding invariants.
"""

import hashlib
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app import config
from app.connectors.base import DocumentConnector, ConnectorDocument
from app.connectors.registry import connector_registry
from app.connectors.secrets import get_secret_provider
from app.connectors.permissions import PermissionNormalizer, NormalizedPermissionSet
from app.connectors.sync.models import (
    SyncMode,
    SyncAction,
    SyncPlan,
    SyncResult,
)
from app.connectors.sync.planner import SyncPlanner
from app.connectors.errors import (
    ConnectorError,
    ConnectorNotFoundError,
    SourceUnavailableError,
    SecretDecryptionError,
    ConcurrentSyncError,
    AuthenticationError,
)
from app.storage.database import SessionLocal
from app.storage.blob import get_document_storage
from app.storage.models import (
    ConnectorConfig as DBConnectorConfig,
    SyncRun as DBSyncRun,
    Document as DBDocument,
    DocumentChunk as DBDocumentChunk,
)
from app.storage.repositories import (
    SQLConnectorRepository,
    SQLSyncRunRepository,
    SQLDocumentRepository,
    SQLPermissionRepository,
    SQLChunkRepository,
    SQLTenantRepository,
)
from app.ingestion.pipeline import ingest_document
from app.security.audit import log_security_event

logger = logging.getLogger(__name__)


class SyncService:
    """
    Coordinates external source synchronization with authoritative PostgreSQL storage
    and derived search indexes.
    """

    def __init__(self, db: Session):
        self.db = db
        self.connector_repo = SQLConnectorRepository(db)
        self.sync_run_repo = SQLSyncRunRepository(db)
        self.doc_repo = SQLDocumentRepository(db)
        self.perm_repo = SQLPermissionRepository(db)
        self.chunk_repo = SQLChunkRepository(db)
        self.tenant_repo = SQLTenantRepository(db)
        self.blob_storage = get_document_storage()
        self.secret_provider = get_secret_provider()

    def sync(
        self,
        connector_id: str,
        tenant_id: str,
        mode: SyncMode = SyncMode.FULL,
        user_id: Optional[str] = None,
        rag_service: Optional[Any] = None,
    ) -> SyncResult:
        """
        Execute a full or incremental sync run for a specific connector with mutual exclusion locking.
        """
        # Check existence first
        existing_conn = self.connector_repo.get_by_id(connector_id, tenant_id)
        if not existing_conn:
            raise ConnectorNotFoundError(f"Connector '{connector_id}' not found in tenant '{tenant_id}'.")

        # 1. Concurrency Control: Acquire exclusive sync lease
        db_connector = self.connector_repo.acquire_sync_lock(
            connector_id=connector_id,
            tenant_id=tenant_id,
            stale_threshold_seconds=getattr(config, "TASK_STALE_THRESHOLD_SECONDS", 300),
        )
        if not db_connector:
            raise ConcurrentSyncError(
                f"Connector '{connector_id}' is already synchronizing in tenant '{tenant_id}'."
            )

        if db_connector.status == "DISABLED":
            self.connector_repo.release_sync_lock(connector_id, tenant_id, "DISABLED")
            self.db.commit()
            raise ConnectorError(f"Connector '{connector_id}' is disabled.")

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        cursor_before = db_connector.sync_cursor

        from app.observability.metrics import (
            connector_sync_started_total,
            connector_sync_completed_total,
            connector_sync_failed_total,
            connector_sync_duration_seconds,
            connector_docs_processed_total,
            connector_permission_changes_total,
            connector_errors_total,
        )
        from app.observability.schemas import ConnectorTelemetry
        from app.observability.logging import structured_logger
        from app.observability.tracing import tracer
        import time

        t_sync_start = time.perf_counter()

        connector_sync_started_total.inc(
            labels={"connector_type": db_connector.connector_type, "sync_mode": mode.value}
        )

        # 2. Record SyncRun started
        sync_run = DBSyncRun(
            id=run_id,
            connector_id=connector_id,
            tenant_id=tenant_id,
            sync_mode=mode.value,
            status="SYNCING",
            started_at=started_at,
            cursor_before=cursor_before,
        )
        self.sync_run_repo.create(sync_run)
        self.db.commit()

        log_security_event(
            event_type="SYNC_STARTED",
            tenant_id=tenant_id,
            user_id=user_id or "system",
            action="sync",
            result="started",
            details={"connector_id": connector_id, "mode": mode.value, "run_id": run_id},
        )

        final_connector_status = "ACTIVE"

        try:
            with tracer.start_span(
                f"connector.sync.{db_connector.connector_type.lower()}",
                attributes={"connector.type": db_connector.connector_type, "sync.mode": mode.value},
            ):
                # 3. Decrypt config and instantiate connector
                try:
                    config_dict = self.secret_provider.decrypt_json(db_connector.encrypted_config)
                    connector: DocumentConnector = connector_registry.create(
                        connector_type=db_connector.connector_type,
                        tenant_id=tenant_id,
                        connector_id=connector_id,
                        config=config_dict,
                    )
                except SecretDecryptionError as s_exc:
                    final_connector_status = "ERROR"
                    return self._fail_sync_run(
                        sync_run=sync_run,
                        db_connector=db_connector,
                        error_msg=f"Secret decryption failed: {s_exc}",
                        user_id=user_id,
                    )
                except Exception as exc:
                    final_connector_status = "ERROR"
                    return self._fail_sync_run(
                        sync_run=sync_run,
                        db_connector=db_connector,
                        error_msg=f"Failed to initialize connector adapter: {exc}",
                        user_id=user_id,
                    )

                # 4. Source Discovery Phase
                is_complete_discovery = True
                max_docs_limit = getattr(config, "MAX_SYNC_DOCUMENTS", 10000)
                discovered_docs: List[ConnectorDocument] = []
                deleted_source_ids: List[str] = []
                new_cursor: Optional[str] = None

                try:
                    if mode == SyncMode.INCREMENTAL:
                        discovered_docs, deleted_source_ids, new_cursor = connector.fetch_changes(cursor=cursor_before)
                    else:
                        discovered_docs = connector.list_documents()
                        # For full sync, if provider has change tracking, get current cursor
                        if hasattr(connector, "fetch_changes"):
                            try:
                                _, _, new_cursor = connector.fetch_changes(cursor=None)
                            except Exception:
                                new_cursor = None

                    if len(discovered_docs) > max_docs_limit:
                        # Discovery exceeded maximum document ceiling -> mark incomplete to prevent inadvertent mass deletions
                        discovered_docs = discovered_docs[:max_docs_limit]
                        is_complete_discovery = False

                except Exception as exc:
                    # Source outage must NEVER cause deletion of existing documents!
                    final_connector_status = "ERROR"
                    return self._fail_sync_run(
                        sync_run=sync_run,
                        db_connector=db_connector,
                        error_msg=f"Source discovery failed: {exc}",
                        user_id=user_id,
                    )

                # 5. Planning Phase
                all_tenant_docs = self.doc_repo.list_by_tenant(tenant_id=tenant_id, limit=max_docs_limit)
                connector_docs = [
                    d for d in all_tenant_docs
                    if (d.metadata_json or {}).get("connector_id") == connector_id
                    or (d.source == db_connector.connector_type and d.source_document_id is not None)
                ]

                plan: SyncPlan = SyncPlanner.plan(
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    source_type=db_connector.connector_type,
                    sync_mode=mode,
                    discovered_docs=discovered_docs,
                    existing_docs=connector_docs,
                    is_complete_discovery=is_complete_discovery,
                    deleted_source_ids=deleted_source_ids,
                )

                # 6. Execution Phase (Transactional Batch Processing)
                documents_added = 0
                documents_updated = 0
                permissions_updated = 0
                documents_skipped = 0
                documents_deleted = 0
                errors: List[Dict[str, Any]] = []

                for item in plan.items:
                    try:
                        if item.action == SyncAction.SKIP:
                            documents_skipped += 1

                        elif item.action == SyncAction.IMPORT:
                            self._execute_import(item.source_doc, db_connector, user_id, rag_service, config_dict)
                            documents_added += 1

                        elif item.action == SyncAction.UPDATE:
                            self._execute_update(item.source_doc, item.document_id, db_connector, user_id, rag_service, config_dict)
                            documents_updated += 1

                        elif item.action == SyncAction.UPDATE_PERMISSIONS:
                            self._execute_permission_update(item.source_doc, item.document_id, db_connector, user_id, config_dict)
                            permissions_updated += 1

                        elif item.action == SyncAction.DELETE:
                            self._execute_delete(item.document_id, tenant_id, user_id, rag_service)
                            documents_deleted += 1

                    except Exception as item_err:
                        self.db.rollback()
                        errors.append({"source_id": item.source_id, "action": item.action.value, "error": str(item_err)})
                        logger.error(f"[SyncService] Error processing item {item.source_id} ({item.action.value}): {item_err}")

                # 7. Token Refresh Check
                try:
                    refreshed_credentials = connector.refresh_credentials()
                    if refreshed_credentials:
                        db_connector.encrypted_config = self.secret_provider.encrypt_json(refreshed_credentials)
                except Exception as t_exc:
                    logger.warning(f"[SyncService] Failed to persist refreshed credentials: {t_exc}")

                # 8. Transactional Cursor & Sync Run Finalization
                completed_at = datetime.now(timezone.utc)
                if errors:
                    final_status = "PARTIAL" if (documents_added or documents_updated or permissions_updated or documents_deleted) else "ERROR"
                else:
                    final_status = "SUCCESS"

                # TRANSACTIONAL CURSOR INVARIANT: Advance cursor ONLY if no fatal errors in batch!
                effective_cursor = new_cursor if (final_status == "SUCCESS" and new_cursor) else cursor_before
                db_connector.sync_cursor = effective_cursor
                db_connector.last_sync_at = completed_at
                final_connector_status = "ACTIVE" if final_status in {"SUCCESS", "PARTIAL"} else "ERROR"

                sync_run.status = final_status
                sync_run.completed_at = completed_at
                sync_run.cursor_after = effective_cursor
                sync_run.documents_seen = len(discovered_docs)
                sync_run.documents_added = documents_added
                sync_run.documents_updated = documents_updated
                sync_run.documents_deleted = documents_deleted
                sync_run.permissions_updated = permissions_updated
                sync_run.documents_skipped = documents_skipped
                sync_run.errors_json = errors
                self.sync_run_repo.update(sync_run)
                self.db.commit()

                duration_ms = (time.perf_counter() - t_sync_start) * 1000

                connector_sync_completed_total.inc(
                    labels={"connector_type": db_connector.connector_type, "status": final_status.lower()}
                )
                connector_sync_duration_seconds.observe(
                    duration_ms / 1000.0,
                    labels={"connector_type": db_connector.connector_type},
                )
                if documents_added:
                    connector_docs_processed_total.inc(amount=float(documents_added), labels={"connector_type": db_connector.connector_type, "action": "import"})
                if documents_updated:
                    connector_docs_processed_total.inc(amount=float(documents_updated), labels={"connector_type": db_connector.connector_type, "action": "update"})
                if documents_deleted:
                    connector_docs_processed_total.inc(amount=float(documents_deleted), labels={"connector_type": db_connector.connector_type, "action": "delete"})
                if permissions_updated:
                    connector_permission_changes_total.inc(amount=float(permissions_updated), labels={"connector_type": db_connector.connector_type, "action": "update_permissions"})
                if errors:
                    connector_errors_total.inc(amount=float(len(errors)), labels={"connector_type": db_connector.connector_type, "error_class": "ItemProcessingError"})

                structured_logger.info(
                    "connector.sync.completed",
                    duration_ms=duration_ms,
                    telemetry_model=ConnectorTelemetry(
                        connector_type=db_connector.connector_type,
                        sync_mode=mode.value,
                        status=final_status.lower(),
                        discovered_count=len(discovered_docs),
                        added_count=documents_added,
                        updated_count=documents_updated,
                        deleted_count=documents_deleted,
                        permissions_updated_count=permissions_updated,
                        duration_ms=duration_ms,
                    ),
                )

                log_security_event(
                    event_type="SYNC_COMPLETED" if final_status == "SUCCESS" else "SYNC_FAILED",
                    tenant_id=tenant_id,
                    user_id=user_id or "system",
                    action="sync",
                    result=final_status.lower(),
                    details={
                        "connector_id": connector_id,
                        "mode": mode.value,
                        "added": documents_added,
                        "updated": documents_updated,
                        "permissions_updated": permissions_updated,
                        "skipped": documents_skipped,
                        "deleted": documents_deleted,
                        "cursor_after": effective_cursor,
                        "error_count": len(errors),
                    },
                )

                return SyncResult(
                    run_id=run_id,
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                    sync_mode=mode,
                    status=final_status,
                    started_at=started_at,
                    completed_at=completed_at,
                    documents_seen=len(discovered_docs),
                    documents_added=documents_added,
                    documents_updated=documents_updated,
                    documents_deleted=documents_deleted,
                    permissions_updated=permissions_updated,
                    documents_skipped=documents_skipped,
                    errors=errors,
                )

        finally:
            # 9. Concurrency Control: Always release sync lock
            self.connector_repo.release_sync_lock(
                connector_id=connector_id,
                tenant_id=tenant_id,
                new_status=final_connector_status,
            )
            self.db.commit()

    def _execute_import(
        self,
        s_doc: ConnectorDocument,
        db_connector: DBConnectorConfig,
        user_id: Optional[str],
        rag_service: Optional[Any],
        connector_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        tenant_id = db_connector.tenant_id
        document_id = s_doc.content_hash
        storage_path = self.blob_storage.save(s_doc.content, s_doc.name, tenant_id)

        # Normalize permissions with anyone/domain fail-closed rules
        norm_perms = PermissionNormalizer.normalize(
            acl=s_doc.acl,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_id=user_id,
            connector_config=connector_config,
        )

        acl_hash = s_doc.acl.canonical_hash() if s_doc.acl else "UNKNOWN"
        meta = dict(s_doc.metadata or {})
        meta.update({
            "connector_id": db_connector.id,
            "acl_hash": acl_hash,
            "allowed_roles": norm_perms.allowed_roles,
            "allowed_user_ids": norm_perms.allowed_user_ids,
            "allowed_groups": norm_perms.allowed_groups,
            "denied_roles": norm_perms.denied_roles,
            "denied_user_ids": norm_perms.denied_user_ids,
            "denied_groups": norm_perms.denied_groups,
        })

        db_doc = DBDocument(
            document_id=document_id,
            tenant_id=tenant_id,
            owner_id=user_id,
            filename=s_doc.name,
            source=db_connector.connector_type,
            source_document_id=s_doc.source_id,
            mime_type=s_doc.mime_type,
            size_bytes=s_doc.size_bytes,
            content_hash=s_doc.content_hash,
            access_level=norm_perms.access_level,
            permission_status=norm_perms.permission_status,
            status="PROCESSING",
            storage_path=storage_path,
            metadata_json=meta,
        )
        self.doc_repo.create(db_doc)
        self.db.commit()

        # Ingestion & Chunking
        chunks = ingest_document(
            storage_path,
            tenant_id=tenant_id,
            owner_id=user_id,
            access_level=norm_perms.access_level,
            allowed_roles=norm_perms.allowed_roles,
            allowed_user_ids=norm_perms.allowed_user_ids,
            permission_status=norm_perms.permission_status,
        )

        for c in chunks:
            c.metadata["allowed_groups"] = norm_perms.allowed_groups
            c.metadata["denied_roles"] = norm_perms.denied_roles
            c.metadata["denied_user_ids"] = norm_perms.denied_user_ids
            c.metadata["denied_groups"] = norm_perms.denied_groups
            c.metadata["connector_id"] = db_connector.id
            c.metadata["source"] = db_connector.connector_type
            c.metadata["source_document_id"] = s_doc.source_id

        self.perm_repo.set_normalized_permissions(
            document_id=document_id,
            tenant_id=tenant_id,
            db_permission_records=norm_perms.db_permission_records,
        )

        db_chunks = [
            DBDocumentChunk(
                chunk_id=c.metadata.get("chunk_id", f"{document_id}_{idx}"),
                document_id=document_id,
                tenant_id=tenant_id,
                chunk_index=idx,
                content_hash=hashlib.sha256(c.page_content.encode("utf-8")).hexdigest(),
                content=c.page_content,
                metadata_json=c.metadata,
            )
            for idx, c in enumerate(chunks)
        ]
        self.chunk_repo.create_batch(db_chunks)
        self.doc_repo.update_status(document_id, status="INDEXING")
        self.db.commit()

        if rag_service:
            if hasattr(rag_service, "vector_db") and hasattr(rag_service.vector_db, "vectordb"):
                rag_service.vector_db.vectordb.add_documents(chunks)
            if hasattr(rag_service, "retrieval_pipeline"):
                rag_service.retrieval_pipeline.rebuild_bm25()

        self.doc_repo.update_status(document_id, status="INDEXED", indexed_at=datetime.now(timezone.utc))
        self.db.commit()

        log_security_event(
            event_type="DOCUMENT_IMPORTED",
            tenant_id=tenant_id,
            user_id=user_id or "system",
            action="import",
            result="success",
            document_id=document_id,
            details={"source_id": s_doc.source_id, "num_chunks": len(chunks)},
        )

    def _execute_update(
        self,
        s_doc: ConnectorDocument,
        document_id: str,
        db_connector: DBConnectorConfig,
        user_id: Optional[str],
        rag_service: Optional[Any],
        connector_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        tenant_id = db_connector.tenant_id
        db_doc = self.doc_repo.get_by_id(document_id, tenant_id)
        if not db_doc:
            return self._execute_import(s_doc, db_connector, user_id, rag_service, connector_config)

        storage_path = self.blob_storage.save(s_doc.content, s_doc.name, tenant_id)
        norm_perms = PermissionNormalizer.normalize(
            acl=s_doc.acl,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_id=user_id or db_doc.owner_id,
            connector_config=connector_config,
        )

        acl_hash = s_doc.acl.canonical_hash() if s_doc.acl else "UNKNOWN"
        meta = dict(db_doc.metadata_json or {})
        meta.update({
            "connector_id": db_connector.id,
            "acl_hash": acl_hash,
            "allowed_roles": norm_perms.allowed_roles,
            "allowed_user_ids": norm_perms.allowed_user_ids,
            "allowed_groups": norm_perms.allowed_groups,
            "denied_roles": norm_perms.denied_roles,
            "denied_user_ids": norm_perms.denied_user_ids,
            "denied_groups": norm_perms.denied_groups,
        })

        db_doc.filename = s_doc.name
        db_doc.content_hash = s_doc.content_hash
        db_doc.size_bytes = s_doc.size_bytes
        db_doc.mime_type = s_doc.mime_type
        db_doc.storage_path = storage_path
        db_doc.access_level = norm_perms.access_level
        db_doc.permission_status = norm_perms.permission_status
        db_doc.metadata_json = meta
        db_doc.status = "PROCESSING"
        db_doc.version += 1
        db_doc.updated_at = datetime.now(timezone.utc)
        self.db.commit()

        chunks = ingest_document(
            storage_path,
            tenant_id=tenant_id,
            owner_id=user_id or db_doc.owner_id,
            access_level=norm_perms.access_level,
            allowed_roles=norm_perms.allowed_roles,
            allowed_user_ids=norm_perms.allowed_user_ids,
            permission_status=norm_perms.permission_status,
        )

        for c in chunks:
            c.metadata["allowed_groups"] = norm_perms.allowed_groups
            c.metadata["denied_roles"] = norm_perms.denied_roles
            c.metadata["denied_user_ids"] = norm_perms.denied_user_ids
            c.metadata["denied_groups"] = norm_perms.denied_groups
            c.metadata["connector_id"] = db_connector.id
            c.metadata["source"] = db_connector.connector_type
            c.metadata["source_document_id"] = s_doc.source_id

        self.perm_repo.set_normalized_permissions(
            document_id=document_id,
            tenant_id=tenant_id,
            db_permission_records=norm_perms.db_permission_records,
        )

        self.chunk_repo.delete_by_document(document_id, tenant_id=tenant_id)
        db_chunks = [
            DBDocumentChunk(
                chunk_id=c.metadata.get("chunk_id", f"{document_id}_{idx}"),
                document_id=document_id,
                tenant_id=tenant_id,
                chunk_index=idx,
                content_hash=hashlib.sha256(c.page_content.encode("utf-8")).hexdigest(),
                content=c.page_content,
                metadata_json=c.metadata,
            )
            for idx, c in enumerate(chunks)
        ]
        self.chunk_repo.create_batch(db_chunks)
        self.doc_repo.update_status(document_id, status="INDEXING")
        self.db.commit()

        if rag_service:
            if hasattr(rag_service, "vector_db") and hasattr(rag_service.vector_db, "vectordb"):
                try:
                    rag_service.vector_db.vectordb.delete(where={"document_id": document_id})
                except Exception:
                    pass
                rag_service.vector_db.vectordb.add_documents(chunks)
            if hasattr(rag_service, "retrieval_pipeline"):
                rag_service.retrieval_pipeline.rebuild_bm25()

        self.doc_repo.update_status(document_id, status="INDEXED", indexed_at=datetime.now(timezone.utc))
        self.db.commit()

        log_security_event(
            event_type="DOCUMENT_UPDATED",
            tenant_id=tenant_id,
            user_id=user_id or "system",
            action="update",
            result="success",
            document_id=document_id,
            details={"source_id": s_doc.source_id, "version": db_doc.version},
        )

    def _execute_permission_update(
        self,
        s_doc: ConnectorDocument,
        document_id: str,
        db_connector: DBConnectorConfig,
        user_id: Optional[str],
        connector_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        In-place permission synchronization WITHOUT re-chunking or re-embedding!
        """
        tenant_id = db_connector.tenant_id
        db_doc = self.doc_repo.get_by_id(document_id, tenant_id)
        if not db_doc:
            return

        norm_perms = PermissionNormalizer.normalize(
            acl=s_doc.acl,
            tenant_id=tenant_id,
            document_id=document_id,
            owner_id=user_id or db_doc.owner_id,
            connector_config=connector_config,
        )

        acl_hash = s_doc.acl.canonical_hash() if s_doc.acl else "UNKNOWN"
        meta = dict(db_doc.metadata_json or {})
        meta.update({
            "connector_id": db_connector.id,
            "acl_hash": acl_hash,
            "allowed_roles": norm_perms.allowed_roles,
            "allowed_user_ids": norm_perms.allowed_user_ids,
            "allowed_groups": norm_perms.allowed_groups,
            "denied_roles": norm_perms.denied_roles,
            "denied_user_ids": norm_perms.denied_user_ids,
            "denied_groups": norm_perms.denied_groups,
        })

        db_doc.access_level = norm_perms.access_level
        db_doc.permission_status = norm_perms.permission_status
        db_doc.metadata_json = meta
        db_doc.version += 1
        db_doc.updated_at = datetime.now(timezone.utc)

        self.perm_repo.set_normalized_permissions(
            document_id=document_id,
            tenant_id=tenant_id,
            db_permission_records=norm_perms.db_permission_records,
        )

        chunks = self.chunk_repo.get_by_document(document_id, tenant_id=tenant_id)
        for c in chunks:
            c_meta = dict(c.metadata_json or {})
            c_meta["access_level"] = norm_perms.access_level
            c_meta["permission_status"] = norm_perms.permission_status
            c_meta["allowed_roles"] = norm_perms.allowed_roles
            c_meta["allowed_user_ids"] = norm_perms.allowed_user_ids
            c_meta["allowed_groups"] = norm_perms.allowed_groups
            c_meta["denied_roles"] = norm_perms.denied_roles
            c_meta["denied_user_ids"] = norm_perms.denied_user_ids
            c_meta["denied_groups"] = norm_perms.denied_groups
            c.metadata_json = c_meta

        self.db.commit()

        log_security_event(
            event_type="PERMISSION_UPDATED",
            tenant_id=tenant_id,
            user_id=user_id or "system",
            action="update_permissions",
            result="success",
            document_id=document_id,
            details={
                "source_id": s_doc.source_id,
                "access_level": norm_perms.access_level,
                "allowed_roles": norm_perms.allowed_roles,
                "denied_roles": norm_perms.denied_roles,
            },
        )

    def _execute_delete(
        self,
        document_id: str,
        tenant_id: str,
        user_id: Optional[str],
        rag_service: Optional[Any],
    ) -> None:
        db_doc = self.doc_repo.get_by_id(document_id, tenant_id)
        if not db_doc:
            return

        if rag_service:
            if hasattr(rag_service, "vector_db") and hasattr(rag_service.vector_db, "vectordb"):
                try:
                    rag_service.vector_db.vectordb.delete(where={"document_id": document_id})
                except Exception:
                    pass
            if hasattr(rag_service, "retrieval_pipeline"):
                rag_service.retrieval_pipeline.rebuild_bm25()

        if db_doc.storage_path:
            self.blob_storage.delete(db_doc.storage_path)

        self.doc_repo.delete(document_id, tenant_id=tenant_id)
        self.db.commit()

        log_security_event(
            event_type="DOCUMENT_DELETED",
            tenant_id=tenant_id,
            user_id=user_id or "system",
            action="delete",
            result="success",
            document_id=document_id,
            details={"deleted_by_sync": True},
        )

    def _fail_sync_run(
        self,
        sync_run: DBSyncRun,
        db_connector: DBConnectorConfig,
        error_msg: str,
        user_id: Optional[str],
    ) -> SyncResult:
        self.db.rollback()
        completed_at = datetime.now(timezone.utc)
        sync_run.status = "ERROR"
        sync_run.completed_at = completed_at
        sync_run.cursor_after = sync_run.cursor_before
        sync_run.errors_json = [{"error": error_msg}]
        self.sync_run_repo.update(sync_run)

        db_connector.status = "ERROR"
        self.db.commit()

        log_security_event(
            event_type="SYNC_FAILED",
            tenant_id=db_connector.tenant_id,
            user_id=user_id or "system",
            action="sync",
            result="error",
            details={"connector_id": db_connector.id, "error": error_msg},
        )

        return SyncResult(
            run_id=sync_run.id,
            connector_id=db_connector.id,
            tenant_id=db_connector.tenant_id,
            sync_mode=SyncMode(sync_run.sync_mode),
            status="ERROR",
            started_at=sync_run.started_at,
            completed_at=completed_at,
            errors=[{"error": error_msg}],
        )
