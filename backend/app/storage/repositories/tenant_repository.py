"""
tenant_repository.py — Tenant repository interface and SQLAlchemy implementation.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
from sqlalchemy.orm import Session

from app.storage.models.tenant import Tenant
from app.storage.repositories.base import DuplicateEntityException


class TenantRepositoryInterface(ABC):
    @abstractmethod
    def get_by_id(self, tenant_id: str) -> Optional[Tenant]:
        pass

    @abstractmethod
    def create(self, tenant: Tenant) -> Tenant:
        pass

    @abstractmethod
    def list_active(self) -> List[Tenant]:
        pass

    @abstractmethod
    def get_or_create(self, tenant_id: str, name: str) -> Tenant:
        pass


class SQLTenantRepository(TenantRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, tenant_id: str) -> Optional[Tenant]:
        return self.db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()

    def create(self, tenant: Tenant) -> Tenant:
        existing = self.get_by_id(tenant.tenant_id)
        if existing:
            raise DuplicateEntityException(f"Tenant with ID '{tenant.tenant_id}' already exists.")
        self.db.add(tenant)
        self.db.flush()
        return tenant

    def list_active(self) -> List[Tenant]:
        return self.db.query(Tenant).filter(Tenant.is_active == True).all()

    def get_or_create(self, tenant_id: str, name: str) -> Tenant:
        tenant = self.get_by_id(tenant_id)
        if not tenant:
            tenant = Tenant(tenant_id=tenant_id, name=name, is_active=True)
            self.db.add(tenant)
            self.db.flush()
        return tenant
