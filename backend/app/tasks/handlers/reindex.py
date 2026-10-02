"""
reindex.py — Document Reindexing and Shadow Index Rebuild Task Handlers.
Reuses IndexRebuilderService to execute zero-downtime shadow index rebuilds
with the full 14-point validation matrix and rollback retention.
"""

import logging
from typing import Dict, Any

from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.models import TaskType
from app.storage.models.task import Task
from app.storage.rebuilder import IndexRebuilderService

logger = logging.getLogger(__name__)


class DocumentReindexHandler(TaskHandler):
    """
    Handler for re-indexing a single document or subset of documents.
    """

    def can_handle(self, task_type: str) -> bool:
        return task_type == TaskType.DOCUMENT_REINDEX.value

    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        payload = task.payload or {}
        document_id = payload.get("document_id")
        tenant_id = task.tenant_id

        if not document_id:
            raise NonRetryableTaskError(f"Missing document_id in DOCUMENT_REINDEX task {task.id}.")

        rebuilder = IndexRebuilderService(context.db, search_store=context.search_store)
        try:
            # Rebuild derived index from PostgreSQL source of truth
            stats = rebuilder.rebuild_from_postgres(tenant_id=tenant_id, batch_size=100)
            return {"status": "SUCCEEDED", "stats": stats}
        except Exception as exc:
            logger.error(f"[ReindexHandler] Document reindex failed on {document_id}: {exc}")
            raise RetryableTaskError(f"Reindex failure: {exc}") from exc


class IndexRebuildHandler(TaskHandler):
    """
    Handler for full zero-downtime cluster shadow index rebuild.
    """

    def can_handle(self, task_type: str) -> bool:
        return task_type == TaskType.INDEX_REBUILD.value

    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        payload = task.payload or {}
        tenant_id = task.tenant_id if task.tenant_id != "global" else None

        rebuilder = IndexRebuilderService(context.db, search_store=context.search_store)
        try:
            stats = rebuilder.rebuild_from_postgres(
                tenant_id=tenant_id,
                batch_size=payload.get("batch_size", 250),
            )
            return {"status": "SUCCEEDED", "rebuild_stats": stats}
        except Exception as exc:
            logger.error(f"[IndexRebuildHandler] Cluster rebuild failed: {exc}")
            raise RetryableTaskError(f"Index rebuild failure: {exc}") from exc
