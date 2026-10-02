"""
chunk_repository.py — Chunk repository interface and SQLAlchemy implementation.
Provides chunk persistence, batch ingestion, and chunk retrieval for derived index rebuilds.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
from sqlalchemy.orm import Session

from app.storage.models.chunk import DocumentChunk
from app.storage.models.document import Document


class ChunkRepositoryInterface(ABC):
    @abstractmethod
    def create_batch(self, chunks: List[DocumentChunk]) -> List[DocumentChunk]:
        pass

    @abstractmethod
    def get_by_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> List[DocumentChunk]:
        pass

    @abstractmethod
    def get_by_tenant(
        self,
        tenant_id: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[DocumentChunk]:
        pass

    @abstractmethod
    def get_indexed_chunks(
        self,
        tenant_id: Optional[str] = None,
    ) -> List[DocumentChunk]:
        pass

    @abstractmethod
    def delete_by_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> int:
        pass

    @abstractmethod
    def count_for_tenant(self, tenant_id: str) -> int:
        pass


class SQLChunkRepository(ChunkRepositoryInterface):
    def __init__(self, db: Session):
        self.db = db

    def create_batch(self, chunks: List[DocumentChunk]) -> List[DocumentChunk]:
        if not chunks:
            return []
        self.db.add_all(chunks)
        self.db.flush()
        return chunks

    def get_by_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> List[DocumentChunk]:
        query = self.db.query(DocumentChunk).filter(DocumentChunk.document_id == document_id)
        if tenant_id:
            query = query.filter(DocumentChunk.tenant_id == tenant_id)
        return query.order_by(DocumentChunk.chunk_index.asc()).all()

    def get_by_tenant(
        self,
        tenant_id: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[DocumentChunk]:
        return (
            self.db.query(DocumentChunk)
            .filter(DocumentChunk.tenant_id == tenant_id)
            .order_by(DocumentChunk.created_at.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def get_indexed_chunks(
        self,
        tenant_id: Optional[str] = None,
    ) -> List[DocumentChunk]:
        """
        Fetch all chunks associated with documents whose status is 'INDEXED'.
        Essential for derived index disaster recovery.
        """
        query = (
            self.db.query(DocumentChunk)
            .join(Document, DocumentChunk.document_id == Document.document_id)
            .filter(Document.status == "INDEXED")
        )
        if tenant_id:
            query = query.filter(DocumentChunk.tenant_id == tenant_id)
        return query.order_by(DocumentChunk.created_at.asc()).all()

    def delete_by_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = None,
    ) -> int:
        query = self.db.query(DocumentChunk).filter(DocumentChunk.document_id == document_id)
        if tenant_id:
            query = query.filter(DocumentChunk.tenant_id == tenant_id)
        count = query.delete(synchronize_session=False)
        self.db.flush()
        return count

    def count_for_tenant(self, tenant_id: str) -> int:
        return self.db.query(DocumentChunk).filter(DocumentChunk.tenant_id == tenant_id).count()
