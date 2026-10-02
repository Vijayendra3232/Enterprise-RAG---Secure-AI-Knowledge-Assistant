"""
deletion.py — Crash-Safe, Idempotent Document Deletion Task Handler.
Cascades deletion across OpenSearch derived indexes, physical blob storage,
and PostgreSQL authoritative tables (document_chunks, document_permissions, documents).
Treats missing records or blobs as idempotent successes.
"""

import logging
from typing import Dict, Any

from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError
from app.tasks.models import TaskType
from app.storage.models.task import Task
from app.storage.repositories.document_repository import SQLDocumentRepository
from app.security.audit import log_security_event

logger = logging.getLogger(__name__)


class DocumentDeleteHandler(TaskHandler):
    """
    Asynchronous document deletion worker handler.
    """

    def can_handle(self, task_type: str) -> bool:
        return task_type == TaskType.DOCUMENT_DELETE.value

    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        payload = task.payload or {}
        document_id = payload.get("document_id")
        tenant_id = task.tenant_id
        user_id = payload.get("user_id", "system")

        if not document_id:
            return {"status": "skipped", "reason": "no_document_id"}

        doc_repo = SQLDocumentRepository(context.db)
        db_doc = doc_repo.get_by_id(document_id, tenant_id=tenant_id)
        storage_path = db_doc.storage_path if db_doc else payload.get("storage_path")

        # ── 1. Delete Chunks from OpenSearch / Search Store ─────────────────
        search_store = context.search_store
        if search_store:
            try:
                search_store.delete_document(document_id, tenant_id=tenant_id)
            except Exception as exc:
                logger.warning(f"[DeleteHandler] Search store document deletion warning on {document_id}: {exc}")

        # ── 2. Delete from Legacy Vector / BM25 Store (if configured) ───────
        if context.rag_service and hasattr(context.rag_service, "vector_db"):
            try:
                rag_service = context.rag_service
                if hasattr(rag_service.vector_db, "vectordb"):
                    rag_service.vector_db.vectordb.delete(where={"document_id": document_id})
                rag_service.retrieval_pipeline.rebuild_bm25()
            except Exception as exc:
                logger.warning(f"[DeleteHandler] Legacy vector deletion warning: {exc}")

        # ── 3. Delete Physical Blob File ────────────────────────────────────
        from app.storage.blob import get_document_storage
        blob_storage = context.blob_storage or get_document_storage()
        if blob_storage and storage_path:
            try:
                blob_storage.delete(storage_path)
            except Exception as exc:
                logger.warning(f"[DeleteHandler] Blob deletion warning on {storage_path}: {exc}")

        # ── 4. Delete Database Record (Cascades to Chunks & Permissions) ────
        doc_repo.delete(document_id, tenant_id=tenant_id)
        context.db.commit()

        # ── 5. Log Security Audit Event ─────────────────────────────────────
        log_security_event(
            event_type="DOCUMENT_DELETE",
            tenant_id=tenant_id,
            user_id=user_id,
            action="async_delete",
            result="success",
            document_id=document_id,
            details={"task_id": task.id},
        )

        return {"status": "DELETED", "document_id": document_id}
