"""
sync_run_repository.py — Repository for tracking connector sync executions and metrics.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
from sqlalchemy.orm import Session

from app.storage.models.sync_run import SyncRun


class SyncRunRepositoryInterface(ABC):
    @abstractmethod
    def create(self, sync_run: SyncRun) -> SyncRun:
        pass

    @abstractmethod
    def get_by_id(self, run_id: str, tenant_id: str) -> Optional[SyncRun]:
        pass

    @abstractmethod
    def get_latest_by_connector(self, connector_id: str, tenant_id: str) -> Optional[SyncRun]:
        pass

    @abstractmethod
    def list_by_connector(
        self,
        connector_id: str,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[SyncRun]:
        pass

    @abstractmethod
    def update(self, sync_run: SyncRun) -> SyncRun:
        pass


class SQLSyncRunRepository(SyncRunRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def create(self, sync_run: SyncRun) -> SyncRun:
        self.db.add(sync_run)
        self.db.flush()
        return sync_run

    def get_by_id(self, run_id: str, tenant_id: str) -> Optional[SyncRun]:
        return (
            self.db.query(SyncRun)
            .filter(
                SyncRun.id == run_id,
                SyncRun.tenant_id == tenant_id,
            )
            .first()
        )

    def get_latest_by_connector(self, connector_id: str, tenant_id: str) -> Optional[SyncRun]:
        return (
            self.db.query(SyncRun)
            .filter(
                SyncRun.connector_id == connector_id,
                SyncRun.tenant_id == tenant_id,
            )
            .order_by(SyncRun.started_at.desc())
            .first()
        )

    def list_by_connector(
        self,
        connector_id: str,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[SyncRun]:
        return (
            self.db.query(SyncRun)
            .filter(
                SyncRun.connector_id == connector_id,
                SyncRun.tenant_id == tenant_id,
            )
            .order_by(SyncRun.started_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def update(self, sync_run: SyncRun) -> SyncRun:
        db_run = self.get_by_id(sync_run.id, sync_run.tenant_id)
        if db_run:
            db_run.status = sync_run.status
            db_run.completed_at = sync_run.completed_at
            db_run.documents_seen = sync_run.documents_seen
            db_run.documents_added = sync_run.documents_added
            db_run.documents_updated = sync_run.documents_updated
            db_run.documents_deleted = sync_run.documents_deleted
            db_run.permissions_updated = sync_run.permissions_updated
            db_run.documents_skipped = sync_run.documents_skipped
            db_run.errors_json = sync_run.errors_json
            self.db.flush()
            return db_run
        return sync_run
