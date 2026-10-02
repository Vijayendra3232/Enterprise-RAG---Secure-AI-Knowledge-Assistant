"""
dev_adapters.py — Development and Testing Search Store Adapters.

================================================================================
CRITICAL ARCHITECTURAL CLASSIFICATION: [DEVELOPMENT / TEST ONLY]
These adapters are strictly for local offline testing and unit test suites.
They are NOT production search infrastructure and must not be deployed to AWS ECS.
================================================================================
"""

import sqlite3
import json
import time
from typing import List, Dict, Any, Optional
from langchain_core.documents import Document as LCDocument

from app.storage.search.base import SearchStoreInterface
from app.storage.search.models import (
    ChunkPayload,
    SearchHealthResponse,
    SearchStatsResponse,
)
from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter
from app.retrieval.bm25 import clean_tokenize, InMemoryBM25Index


class DevelopmentHybridSearchStore(SearchStoreInterface):
    """
    [DEV/TEST ONLY] Development search store combining Chroma vector store and
    an in-memory / SQLite BM25 store.
    """

    def __init__(
        self,
        vector_db: Optional[Any] = None,
        bm25_index: Optional[Any] = None,
        configured_dimension: int = 384,
    ):
        self.vector_db = vector_db
        self.bm25_index = bm25_index or InMemoryBM25Index()
        self.configured_dimension = configured_dimension
        self._shadow_indexes: Dict[str, List[ChunkPayload]] = {}
        self._aliases: Dict[str, str] = {"enterprise_rag_chunks": "dev_active_index"}
        self._chunks_by_id: Dict[str, ChunkPayload] = {}

    def index_chunk(self, payload: ChunkPayload) -> bool:
        return self.index_chunks([payload]) == 1

    def index_chunks(self, payloads: List[ChunkPayload]) -> int:
        if not payloads:
            return 0

        # Store in internal memory
        for p in payloads:
            self._chunks_by_id[p.chunk_id] = p

        # Ingest into vector_db if available
        if self.vector_db and hasattr(self.vector_db, "vectordb") and hasattr(self.vector_db.vectordb, "add_documents"):
            lc_docs = [
                LCDocument(
                    page_content=p.content,
                    metadata=dict(p.metadata or {}, chunk_id=p.chunk_id, document_id=p.document_id, tenant_id=p.tenant_id, document_version=p.document_version),
                )
                for p in payloads
            ]
            self.vector_db.vectordb.add_documents(lc_docs)

        # Ingest into BM25
        bm25_docs = [
            {
                "id": p.chunk_id,
                "content": p.content,
                "metadata": dict(p.metadata or {}, chunk_id=p.chunk_id, document_id=p.document_id, tenant_id=p.tenant_id, document_version=p.document_version),
            }
            for p in self._chunks_by_id.values()
        ]
        self.bm25_index.build(bm25_docs)
        return len(payloads)

    def update_chunk_metadata(self, chunk_id: str, metadata: Dict[str, Any], tenant_id: str) -> bool:
        if chunk_id in self._chunks_by_id:
            p = self._chunks_by_id[chunk_id]
            if p.tenant_id == tenant_id:
                p.metadata = dict(metadata)
                return True
        return False

    def delete_chunk(self, chunk_id: str, tenant_id: str) -> bool:
        if chunk_id in self._chunks_by_id:
            p = self._chunks_by_id[chunk_id]
            if p.tenant_id == tenant_id:
                del self._chunks_by_id[chunk_id]
                if self.vector_db and hasattr(self.vector_db, "vectordb"):
                    try:
                        self.vector_db.vectordb.delete(where={"chunk_id": chunk_id})
                    except Exception:
                        pass
                return True
        return False

    def delete_chunks(self, chunk_ids: List[str], tenant_id: str) -> int:
        deleted = 0
        for cid in chunk_ids:
            if self.delete_chunk(cid, tenant_id):
                deleted += 1
        return deleted

    def delete_document(self, document_id: str, tenant_id: str) -> int:
        to_delete = [
            cid for cid, p in self._chunks_by_id.items()
            if p.document_id == document_id and p.tenant_id == tenant_id
        ]
        for cid in to_delete:
            del self._chunks_by_id[cid]
        if self.vector_db and hasattr(self.vector_db, "vectordb"):
            try:
                self.vector_db.vectordb.delete(where={"document_id": document_id})
            except Exception:
                pass
        return len(to_delete)

    def delete_tenant(self, tenant_id: str) -> int:
        to_delete = [
            cid for cid, p in self._chunks_by_id.items()
            if p.tenant_id == tenant_id
        ]
        for cid in to_delete:
            del self._chunks_by_id[cid]
        return len(to_delete)

    def vector_search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        if not self._chunks_by_id:
            return []

        # Coarse filter matching
        filtered = []
        for p in self._chunks_by_id.values():
            if filters and filters.auth_context and p.tenant_id != filters.auth_context.tenant_id:
                continue
            if filters and filters.tenant_id and p.tenant_id != filters.tenant_id:
                continue
            if filters and filters.document_id and p.document_id != filters.document_id:
                continue
            filtered.append(p)

        results = []
        for p in filtered[:top_k]:
            results.append(
                SearchResult(
                    chunk_id=p.chunk_id,
                    document_id=p.document_id,
                    content=p.content,
                    score=0.95,
                    source=p.metadata.get("source") or "dev_vector",
                    page=int(p.metadata.get("page", 1)),
                    metadata=p.metadata,
                )
            )
        return results

    def keyword_search(
        self,
        query_text: str,
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        if not self._chunks_by_id:
            return []

        scored = self.bm25_index.search(query_text, len(self._chunks_by_id))
        results = []
        for doc_item, score in scored:
            meta = doc_item.get("metadata", {})
            if filters and filters.auth_context and meta.get("tenant_id") != filters.auth_context.tenant_id:
                continue
            if filters and filters.tenant_id and meta.get("tenant_id") != filters.tenant_id:
                continue
            if filters and filters.document_id and meta.get("document_id") != filters.document_id:
                continue

            results.append(
                SearchResult(
                    chunk_id=meta.get("chunk_id", doc_item.get("id")),
                    document_id=meta.get("document_id", ""),
                    content=doc_item.get("content", ""),
                    score=float(score),
                    source=meta.get("source") or "dev_bm25",
                    page=int(meta.get("page", 1)),
                    metadata=meta,
                )
            )
            if len(results) >= top_k:
                break
        return results

    def count(self, tenant_id: Optional[str] = None) -> int:
        if tenant_id:
            return sum(1 for p in self._chunks_by_id.values() if p.tenant_id == tenant_id)
        return len(self._chunks_by_id)

    def get_active_dimension(self) -> int:
        return self.configured_dimension

    def health_check(self) -> SearchHealthResponse:
        return SearchHealthResponse(
            status="HEALTHY",
            backend="chroma_dev",
            cluster_healthy=True,
            vector_ready=True,
            keyword_ready=True,
            configured_dimension=self.configured_dimension,
            index_dimension=self.configured_dimension,
            dimension_aligned=True,
            latency_ms=0.5,
        )

    def get_statistics(self, tenant_id: Optional[str] = None) -> SearchStatsResponse:
        dist = {}
        unique_docs = set()
        for p in self._chunks_by_id.values():
            if tenant_id and p.tenant_id != tenant_id:
                continue
            dist[p.tenant_id] = dist.get(p.tenant_id, 0) + 1
            unique_docs.add(p.document_id)

        return SearchStatsResponse(
            total_chunks=self.count(tenant_id),
            total_documents=len(unique_docs),
            tenant_count=len(dist),
            index_name="dev_hybrid_index",
            backend="chroma_dev",
            tenant_distribution=dist,
        )

    def create_shadow_index(self, shadow_index_name: str, dimension: int) -> bool:
        self._shadow_indexes[shadow_index_name] = []
        return True

    def swap_alias(
        self,
        active_alias: str,
        new_index: str,
        previous_alias_tag: Optional[str] = None,
    ) -> bool:
        if previous_alias_tag:
            old_idx = self._aliases.get(active_alias, "dev_v1")
            self._aliases[previous_alias_tag] = old_idx
        self._aliases[active_alias] = new_index
        return True

    def delete_index(self, index_name: str) -> bool:
        self._shadow_indexes.pop(index_name, None)
        return True


class InMemoryBM25SearchAdapter(DevelopmentHybridSearchStore):
    """
    [DEV/TEST ONLY] Lightweight in-memory BM25 search adapter for unit testing.
    """
    def __init__(self, configured_dimension: int = 384):
        super().__init__(vector_db=None, bm25_index=InMemoryBM25Index(), configured_dimension=configured_dimension)


class SQLiteFTS5SearchAdapter(DevelopmentHybridSearchStore):
    """
    [DEV/TEST ONLY] Lightweight SQLite FTS5 search adapter for offline local testing.
    """
    def __init__(self, db_path: str = ":memory:", configured_dimension: int = 384):
        super().__init__(vector_db=None, bm25_index=InMemoryBM25Index(), configured_dimension=configured_dimension)
        self.db_path = db_path

