"""
handlers — Task handlers package exporting specialized idempotent handlers.
"""

from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.handlers.ingestion import DocumentIngestHandler
from app.tasks.handlers.deletion import DocumentDeleteHandler
from app.tasks.handlers.permission_sync import PermissionSyncHandler
from app.tasks.handlers.connector_sync import ConnectorSyncHandler
from app.tasks.handlers.reindex import DocumentReindexHandler, IndexRebuildHandler

__all__ = [
    "TaskHandler",
    "WorkerContext",
    "RetryableTaskError",
    "NonRetryableTaskError",
    "DocumentIngestHandler",
    "DocumentDeleteHandler",
    "PermissionSyncHandler",
    "ConnectorSyncHandler",
    "DocumentReindexHandler",
    "IndexRebuildHandler",
]
