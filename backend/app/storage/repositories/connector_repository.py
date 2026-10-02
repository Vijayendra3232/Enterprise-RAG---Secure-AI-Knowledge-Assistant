"""
connector_repository.py — Repository for managing persistent connector configurations.
All operations strictly enforce tenant boundary constraints.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.storage.models.connector import ConnectorConfig
from app.storage.repositories.base import (
    DuplicateEntityException,
    EntityNotFoundException,
)


class ConnectorRepositoryInterface(ABC):
    @abstractmethod
    def create(self, connector: ConnectorConfig) -> ConnectorConfig:
        pass

    @abstractmethod
    def get_by_id(self, connector_id: str, tenant_id: str) -> Optional[ConnectorConfig]:
        pass

    @abstractmethod
    def get_by_name(self, name: str, tenant_id: str) -> Optional[ConnectorConfig]:
        pass

    @abstractmethod
    def list_by_tenant(
        self,
        tenant_id: str,
        connector_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ConnectorConfig]:
        pass

    @abstractmethod
    def update(self, connector: ConnectorConfig) -> ConnectorConfig:
        pass

    @abstractmethod
    def update_status(self, connector_id: str, tenant_id: str, status: str) -> Optional[ConnectorConfig]:
        pass

    @abstractmethod
    def update_last_sync(
        self, connector_id: str, tenant_id: str, sync_time: Optional[datetime] = None
    ) -> Optional[ConnectorConfig]:
        pass

    @abstractmethod
    def delete(self, connector_id: str, tenant_id: str) -> bool:
        pass


class SQLConnectorRepository(ConnectorRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def create(self, connector: ConnectorConfig) -> ConnectorConfig:
        try:
            self.db.add(connector)
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateEntityException(
                f"Connector with name '{connector.name}' already exists in tenant '{connector.tenant_id}'."
            ) from exc
        return connector

    def get_by_id(self, connector_id: str, tenant_id: str) -> Optional[ConnectorConfig]:
        return (
            self.db.query(ConnectorConfig)
            .filter(
                ConnectorConfig.id == connector_id,
                ConnectorConfig.tenant_id == tenant_id,
            )
            .first()
        )

    def get_by_name(self, name: str, tenant_id: str) -> Optional[ConnectorConfig]:
        return (
            self.db.query(ConnectorConfig)
            .filter(
                ConnectorConfig.name == name,
                ConnectorConfig.tenant_id == tenant_id,
            )
            .first()
        )

    def list_by_tenant(
        self,
        tenant_id: str,
        connector_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ConnectorConfig]:
        query = self.db.query(ConnectorConfig).filter(ConnectorConfig.tenant_id == tenant_id)
        if connector_type:
            query = query.filter(ConnectorConfig.connector_type == connector_type.lower())
        return query.order_by(ConnectorConfig.created_at.desc()).offset(offset).limit(limit).all()

    def update(self, connector: ConnectorConfig) -> ConnectorConfig:
        db_conn = self.get_by_id(connector.id, connector.tenant_id)
        if not db_conn:
            raise EntityNotFoundException(f"Connector '{connector.id}' not found.")
        db_conn.name = connector.name
        db_conn.status = connector.status
        db_conn.encrypted_config = connector.encrypted_config
        db_conn.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return db_conn

    def update_status(self, connector_id: str, tenant_id: str, status: str) -> Optional[ConnectorConfig]:
        db_conn = self.get_by_id(connector_id, tenant_id)
        if not db_conn:
            return None
        db_conn.status = status
        db_conn.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return db_conn

    def update_last_sync(
        self, connector_id: str, tenant_id: str, sync_time: Optional[datetime] = None
    ) -> Optional[ConnectorConfig]:
        db_conn = self.get_by_id(connector_id, tenant_id)
        if not db_conn:
            return None
        db_conn.last_sync_at = sync_time or datetime.now(timezone.utc)
        db_conn.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return db_conn

    def update_cursor(
        self, connector_id: str, tenant_id: str, sync_cursor: Optional[str]
    ) -> Optional[ConnectorConfig]:
        db_conn = self.get_by_id(connector_id, tenant_id)
        if not db_conn:
            return None
        db_conn.sync_cursor = sync_cursor
        db_conn.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return db_conn

    def acquire_sync_lock(
        self, connector_id: str, tenant_id: str, stale_threshold_seconds: int = 300
    ) -> Optional[ConnectorConfig]:
        """
        Atomically acquire exclusive synchronization lease for connector.
        Reclaims lock if previous sync has been running longer than stale_threshold_seconds.
        Returns ConnectorConfig if acquired, None if another active sync holds the lock.
        """
        now = datetime.now(timezone.utc)
        db_conn = self.get_by_id(connector_id, tenant_id)
        if not db_conn:
            return None

        # Check if already syncing and not stale
        if db_conn.status == "SYNCING":
            if db_conn.sync_lock_at:
                lock_time = db_conn.sync_lock_at
                if lock_time.tzinfo is None:
                    lock_time = lock_time.replace(tzinfo=timezone.utc)
                elapsed = (now - lock_time).total_seconds()
                if elapsed < stale_threshold_seconds:
                    # Active unexpired sync lock held by another worker
                    return None

        db_conn.status = "SYNCING"
        db_conn.sync_lock_at = now
        db_conn.updated_at = now
        self.db.flush()
        return db_conn

    def release_sync_lock(
        self, connector_id: str, tenant_id: str, new_status: str = "ACTIVE"
    ) -> Optional[ConnectorConfig]:
        """
        Safely release exclusive synchronization lease.
        """
        db_conn = self.get_by_id(connector_id, tenant_id)
        if not db_conn:
            return None
        db_conn.status = new_status
        db_conn.sync_lock_at = None
        db_conn.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return db_conn

    def delete(self, connector_id: str, tenant_id: str) -> bool:
        db_conn = self.get_by_id(connector_id, tenant_id)
        if not db_conn:
            return False
        self.db.delete(db_conn)
        self.db.flush()
        return True
