"""
permission_repository.py — Document permission repository interface and SQLAlchemy implementation.
Provides atomic permission updates, role-to-document bindings, and batch permission resolution.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session

from app.storage.models.permission import DocumentPermission


class PermissionRepositoryInterface(ABC):
    @abstractmethod
    def set_permissions(
        self,
        document_id: str,
        tenant_id: str,
        roles: List[str],
        user_ids: List[str],
        groups: Optional[List[str]] = None,
        permission: str = "DOCUMENT_READ",
        effect: str = "ALLOW",
    ) -> List[DocumentPermission]:
        pass

    @abstractmethod
    def set_normalized_permissions(
        self,
        document_id: str,
        tenant_id: str,
        db_permission_records: List[Dict[str, Any]],
    ) -> List[DocumentPermission]:
        pass

    @abstractmethod
    def get_for_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> List[DocumentPermission]:
        pass

    @abstractmethod
    def batch_get_for_documents(
        self,
        document_ids: List[str],
        tenant_id: Optional[str] = None,
    ) -> Dict[str, List[DocumentPermission]]:
        pass

    @abstractmethod
    def delete_for_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> int:
        pass


class SQLPermissionRepository(PermissionRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def set_permissions(
        self,
        document_id: str,
        tenant_id: str,
        roles: List[str],
        user_ids: List[str],
        groups: Optional[List[str]] = None,
        permission: str = "DOCUMENT_READ",
        effect: str = "ALLOW",
    ) -> List[DocumentPermission]:
        # Delete existing permissions for document
        self.delete_for_document(document_id, tenant_id=tenant_id)

        new_permissions: List[DocumentPermission] = []
        for role in roles:
            if role:
                p = DocumentPermission(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    role=role.upper(),
                    user_id=None,
                    group_id=None,
                    permission=permission,
                    effect=effect,
                )
                self.db.add(p)
                new_permissions.append(p)

        for uid in user_ids:
            if uid:
                p = DocumentPermission(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    role=None,
                    user_id=uid,
                    group_id=None,
                    permission=permission,
                    effect=effect,
                )
                self.db.add(p)
                new_permissions.append(p)

        for gid in (groups or []):
            if gid:
                p = DocumentPermission(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    role=None,
                    user_id=None,
                    group_id=gid.lower(),
                    permission=permission,
                    effect=effect,
                )
                self.db.add(p)
                new_permissions.append(p)

        self.db.flush()
        return new_permissions

    def set_normalized_permissions(
        self,
        document_id: str,
        tenant_id: str,
        db_permission_records: List[Dict[str, Any]],
    ) -> List[DocumentPermission]:
        self.delete_for_document(document_id, tenant_id=tenant_id)
        new_permissions: List[DocumentPermission] = []

        for rec in db_permission_records:
            p = DocumentPermission(
                document_id=document_id,
                tenant_id=tenant_id,
                role=rec.get("role"),
                user_id=rec.get("user_id"),
                group_id=rec.get("group_id"),
                permission=rec.get("permission") or "DOCUMENT_READ",
                effect=rec.get("effect") or "ALLOW",
            )
            self.db.add(p)
            new_permissions.append(p)

        self.db.flush()
        return new_permissions

    def get_for_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> List[DocumentPermission]:
        query = self.db.query(DocumentPermission).filter(DocumentPermission.document_id == document_id)
        if tenant_id:
            query = query.filter(DocumentPermission.tenant_id == tenant_id)
        return query.all()

    def batch_get_for_documents(
        self,
        document_ids: List[str],
        tenant_id: Optional[str] = None,
    ) -> Dict[str, List[DocumentPermission]]:
        if not document_ids:
            return {}
        query = self.db.query(DocumentPermission).filter(DocumentPermission.document_id.in_(document_ids))
        if tenant_id:
            query = query.filter(DocumentPermission.tenant_id == tenant_id)
        records = query.all()

        result: Dict[str, List[DocumentPermission]] = {doc_id: [] for doc_id in document_ids}
        for rec in records:
            if rec.document_id in result:
                result[rec.document_id].append(rec)
        return result

    def delete_for_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> int:
        query = self.db.query(DocumentPermission).filter(DocumentPermission.document_id == document_id)
        if tenant_id:
            query = query.filter(DocumentPermission.tenant_id == tenant_id)
        count = query.delete(synchronize_session=False)
        self.db.flush()
        return count
