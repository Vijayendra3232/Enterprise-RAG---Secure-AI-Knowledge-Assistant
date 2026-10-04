"""
base.py — Abstract Task Handler contract and Worker Context definitions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

from app.storage.models.task import Task
from app.storage.search.base import SearchStoreInterface
from app.storage.blob import DocumentStorageInterface
from app.connectors.secrets import SecretProviderInterface


class RetryableTaskError(Exception):
    """Exception indicating a transient failure that should be retried with exponential backoff."""
    pass


class NonRetryableTaskError(Exception):
    """Exception indicating a permanent failure that should not be retried."""
    pass


@dataclass
class WorkerContext:
    """
    Context passed to task handlers during execution.
    Provides managed database sessions, storage instances, and search providers.
    """
    db: Session
    search_store: Optional[SearchStoreInterface] = None
    blob_storage: Optional[DocumentStorageInterface] = None
    secret_provider: Optional[SecretProviderInterface] = None
    rag_service: Optional[Any] = None
    worker_id: str = "worker-default"

    def heartbeat(self, task_id: str) -> None:
        """Update task.updated_at to current timestamp to prevent stale recovery during active processing."""
        if not task_id or not self.db:
            return
        try:
            from datetime import datetime, timezone
            from app.storage.models.task import Task
            now = datetime.now(timezone.utc)
            self.db.query(Task).filter(Task.id == task_id).update({"updated_at": now})
            self.db.commit()
        except Exception:
            self.db.rollback()


class TaskHandler(ABC):
    """
    Abstract contract for specialized idempotent task handlers.
    """

    @abstractmethod
    def can_handle(self, task_type: str) -> bool:
        """Check if this handler supports the given task type."""
        pass

    @abstractmethod
    def handle(self, task: Task, context: WorkerContext) -> Dict[str, Any]:
        """
        Execute task logic idempotently.
        Returns result metadata dictionary on success.
        Raises RetryableTaskError for transient failures or NonRetryableTaskError for permanent errors.
        """
        pass
