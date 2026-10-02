"""
document_repository.py — Document repository interface and SQLAlchemy implementation.
Supports optimistic concurrency control, source idempotency, and status lifecycle transitions.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.storage.models.document import Document
from app.storage.repositories.base import (
    DuplicateEntityException,
    EntityNotFoundException,
    ConcurrencyConflictException,
)


class DocumentRepositoryInterface(ABC):
    @abstractmethod
    def create(self, doc: Document) -> Document:
        pass

    @abstractmethod
    def get_by_id(self, document_id: str, tenant_id: Optional[str] = None) -> Optional[Document]:
        pass

    @abstractmethod
    def get_by_source(self, tenant_id: str, source: str, source_document_id: str) -> Optional[Document]:
        pass

    @abstractmethod
    def get_by_content_hash(self, tenant_id: str, content_hash: str) -> Optional[Document]:
        pass

    @abstractmethod
    def update_status(
        self,
        document_id: str,
        status: str,
        error_message: Optional[str] = None,
        indexed_at: Optional[datetime] = None,
    ) -> Optional[Document]:
        pass

    @abstractmethod
    def update_document(self, doc: Document, expected_version: Optional[int] = None) -> Document:
        pass

    @abstractmethod
    def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Document]:
        pass

    @abstractmethod
    def delete(self, document_id: str, tenant_id: Optional[str] = None) -> bool:
        pass


class SQLDocumentRepository(DocumentRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def create(self, doc: Document) -> Document:
        existing = self.get_by_id(doc.document_id)
        if existing:
            raise DuplicateEntityException(f"Document with ID '{doc.document_id}' already exists.")

        try:
            self.db.add(doc)
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateEntityException(f"Document unique constraint violated: {exc}") from exc
        return doc

    def get_by_id(self, document_id: str, tenant_id: Optional[str] = None) -> Optional[Document]:
        query = self.db.query(Document).filter(Document.document_id == document_id)
        if tenant_id:
            query = query.filter(Document.tenant_id == tenant_id)
        return query.first()

    def get_by_source(self, tenant_id: str, source: str, source_document_id: str) -> Optional[Document]:
        return (
            self.db.query(Document)
            .filter(
                Document.tenant_id == tenant_id,
                Document.source == source,
                Document.source_document_id == source_document_id,
            )
            .first()
        )

    def get_by_content_hash(self, tenant_id: str, content_hash: str) -> Optional[Document]:
        return (
            self.db.query(Document)
            .filter(
                Document.tenant_id == tenant_id,
                Document.content_hash == content_hash,
            )
            .first()
        )

    def update_status(
        self,
        document_id: str,
        status: str,
        error_message: Optional[str] = None,
        indexed_at: Optional[datetime] = None,
    ) -> Optional[Document]:
        doc = self.get_by_id(document_id)
        if not doc:
            return None
        doc.status = status
        doc.error_message = error_message
        if indexed_at is not None:
            doc.indexed_at = indexed_at
        elif status == "INDEXED" and doc.indexed_at is None:
            doc.indexed_at = datetime.now(timezone.utc)
        doc.updated_at = datetime.now(timezone.utc)
        doc.version += 1
        self.db.flush()
        return doc

    def update_document(self, doc: Document, expected_version: Optional[int] = None) -> Document:
        db_doc = self.get_by_id(doc.document_id)
        if not db_doc:
            raise EntityNotFoundException(f"Document '{doc.document_id}' not found.")

        if expected_version is not None and db_doc.version != expected_version:
            raise ConcurrencyConflictException(
                f"Optimistic concurrency conflict on document '{doc.document_id}'. "
                f"Expected version {expected_version}, but found {db_doc.version}."
            )

        db_doc.filename = doc.filename
        db_doc.source = doc.source
        db_doc.source_document_id = doc.source_document_id
        db_doc.mime_type = doc.mime_type
        db_doc.size_bytes = doc.size_bytes
        db_doc.content_hash = doc.content_hash
        db_doc.access_level = doc.access_level
        db_doc.permission_status = doc.permission_status
        db_doc.status = doc.status
        db_doc.error_message = doc.error_message
        db_doc.storage_path = doc.storage_path
        db_doc.metadata_json = doc.metadata_json
        db_doc.indexed_at = doc.indexed_at
        db_doc.updated_at = datetime.now(timezone.utc)
        db_doc.version += 1
        self.db.flush()
        return db_doc

    def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Document]:
        query = self.db.query(Document).filter(Document.tenant_id == tenant_id)
        if status:
            query = query.filter(Document.status == status)
        return query.order_by(Document.created_at.desc()).offset(offset).limit(limit).all()

    def delete(self, document_id: str, tenant_id: Optional[str] = None) -> bool:
        doc = self.get_by_id(document_id, tenant_id=tenant_id)
        if not doc:
            return False
        self.db.delete(doc)
        self.db.flush()
        return True
