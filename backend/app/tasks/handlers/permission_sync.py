"""
permission_sync.py — Permission Synchronization Task Handler.
Performs in-place metadata updates in PostgreSQL and OpenSearch.
STRICT GUARANTEE: Performs ZERO re-embedding and ZERO re-chunking.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.models import TaskType
from app.storage.models.task import Task
from app.storage.repositories.document_repository import SQLDocumentRepository
from app.storage.repositories.permission_repository import SQLPermissionRepository
from app.storage.repositories.chunk_repository import SQLChunkRepository
from app.security.audit import log_security_event

logger = logging.getLogger(__name__)


class PermissionSyncHandler(TaskHandler):
    """
    Permission-only synchronization handler executing fast metadata updates
    without re-embedding.
    """

    def can_handle(self, task_type: str) -> bool:
        return task_type == TaskType.PERMISSION_SYNC.value

    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        payload = task.payload or {}
        document_id = payload.get("document_id")
        tenant_id = task.tenant_id
        access_level = payload.get("access_level")
        allowed_roles = payload.get("allowed_roles", [])
        allowed_user_ids = payload.get("allowed_user_ids", [])
        user_id = payload.get("user_id", "system")

        if not document_id:
            raise NonRetryableTaskError(f"Missing document_id in PERMISSION_SYNC task {task.id}.")

        doc_repo = SQLDocumentRepository(context.db)
        perm_repo = SQLPermissionRepository(context.db)
        chunk_repo = SQLChunkRepository(context.db)

        db_doc = doc_repo.get_by_id(document_id, tenant_id=tenant_id)
        if not db_doc:
            logger.info(f"[PermSyncHandler] Document '{document_id}' not found. Aborting.")
            return {"status": "aborted", "reason": "document_not_found"}

        # ── 1. Update PostgreSQL Permissions & Chunks Metadata ──────────────
        if access_level:
            db_doc.access_level = access_level.upper()

        perm_repo.set_permissions(
            document_id=document_id,
            tenant_id=tenant_id,
            roles=allowed_roles,
            user_ids=allowed_user_ids,
        )

        chunks = chunk_repo.get_by_document(document_id, tenant_id=tenant_id)
        for c in chunks:
            meta = dict(c.metadata_json or {})
            if access_level:
                meta["access_level"] = access_level.upper()
            meta["allowed_roles"] = allowed_roles
            meta["allowed_user_ids"] = allowed_user_ids
            c.metadata_json = meta

        db_doc.version += 1
        db_doc.updated_at = datetime.now(timezone.utc)
        context.db.commit()

        # ── 2. In-Place OpenSearch Metadata Update (Zero Re-Embedding) ──────
        search_store = context.search_store
        if search_store:
            try:
                for c in chunks:
                    new_meta = {
                        "access_level": db_doc.access_level,
                        "allowed_roles": allowed_roles,
                        "allowed_user_ids": allowed_user_ids,
                    }
                    search_store.update_chunk_metadata(
                        chunk_id=c.chunk_id,
                        metadata=new_meta,
                        tenant_id=tenant_id,
                    )
            except Exception as exc:
                logger.error(f"[PermSyncHandler] OpenSearch metadata update error: {exc}")
                raise RetryableTaskError(f"OpenSearch metadata update failure: {exc}") from exc

        # ── 3. Log Security Audit Event ─────────────────────────────────────
        log_security_event(
            event_type="PERMISSION_UPDATE",
            tenant_id=tenant_id,
            user_id=user_id,
            action="async_permission_sync",
            result="success",
            document_id=document_id,
            details={
                "task_id": task.id,
                "version": db_doc.version,
                "access_level": db_doc.access_level,
                "roles": allowed_roles,
                "user_ids": allowed_user_ids,
                "reembedding_performed": False,
            },
        )

        return {
            "status": "SUCCEEDED",
            "document_id": document_id,
            "version": db_doc.version,
            "chunks_updated": len(chunks),
        }
