"""
connector_sync.py — Connector Synchronization Task Handler.
Integrates the existing Step 8/8.1 connector sync architecture with the asynchronous worker pool.
Strictly preserves source-outage safeguards and anti-mass-deletion protections.
"""

import logging
from typing import Dict, Any

from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.models import TaskType
from app.storage.models.task import Task
from app.connectors.sync.service import SyncService
from app.connectors.sync.models import SyncMode
from app.connectors.errors import (
    ConnectorError,
    ConnectorNotFoundError,
    SourceUnavailableError,
    ConcurrentSyncError,
    PermanentAuthError,
)

logger = logging.getLogger(__name__)


class ConnectorSyncHandler(TaskHandler):
    """
    Asynchronous connector sync worker handler.
    """

    def can_handle(self, task_type: str) -> bool:
        return task_type == TaskType.CONNECTOR_SYNC.value

    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        payload = task.payload or {}
        connector_id = payload.get("connector_id")
        tenant_id = task.tenant_id
        mode_str = payload.get("mode", "FULL").upper()
        mode = SyncMode.INCREMENTAL if mode_str == "INCREMENTAL" else SyncMode.FULL
        user_id = payload.get("user_id", "system")

        if not connector_id:
            raise NonRetryableTaskError(f"Missing connector_id in CONNECTOR_SYNC task {task.id}.")

        sync_service = SyncService(context.db)
        try:
            result = sync_service.sync(
                connector_id=connector_id,
                tenant_id=tenant_id,
                mode=mode,
                user_id=user_id,
                rag_service=context.rag_service,
            )
            return {
                "status": result.status,
                "run_id": result.run_id,
                "documents_seen": result.documents_seen,
                "documents_added": result.documents_added,
                "documents_updated": result.documents_updated,
                "documents_deleted": result.documents_deleted,
                "permissions_updated": result.permissions_updated,
                "documents_skipped": result.documents_skipped,
                "errors": result.errors,
            }
        except ConnectorNotFoundError as exc:
            raise NonRetryableTaskError(f"Connector not found: {exc}") from exc
        except PermanentAuthError as exc:
            raise NonRetryableTaskError(f"Permanent authentication failure: {exc}") from exc
        except ConcurrentSyncError as exc:
            raise RetryableTaskError(f"Connector already syncing, retrying later: {exc}") from exc
        except SourceUnavailableError as exc:
            # Source outage is safe and should be retried
            raise RetryableTaskError(f"Connector source unavailable: {exc}") from exc
        except ConnectorError as exc:
            raise RetryableTaskError(f"Connector sync error: {exc}") from exc
        except Exception as exc:
            logger.error(f"[ConnectorSyncHandler] Unexpected error on {connector_id}: {exc}")
            raise RetryableTaskError(f"Unexpected sync failure: {exc}") from exc
