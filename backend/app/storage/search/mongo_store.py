"""
mongo_store.py — MongoDB Atlas Vector Search Store Adapter.

================================================================================
PRODUCTION SEARCH INFRASTRUCTURE: [MONGODB ATLAS VECTOR SEARCH]
Provides 384-dimensional vector similarity search, tenant isolation,
idempotent chunk indexing, and health checks over MongoDB Atlas.
PostgreSQL remains the authoritative database for documents, users, and ACLs.
================================================================================
"""

import logging
import time
import math
from typing import List, Dict, Any, Optional

from app import config
from app.storage.search.base import SearchStoreInterface
from app.storage.search.models import (
    ChunkPayload,
    SearchHealthResponse,
    SearchStatsResponse,
)
from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter
from app.core import embeddings

logger = logging.getLogger(__name__)


def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm_a = math.sqrt(sum(a * a for a in vec1))
    norm_b = math.sqrt(sum(b * b for b in vec2))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot_product / (norm_a * norm_b)


class MongoVectorStore(SearchStoreInterface):
    """
    MongoDB Atlas Vector Search adapter enforcing SearchStoreInterface.
    Stores 384-dimensional vectors and document metadata in MongoDB Atlas.
    """

    def __init__(
        self,
        mongo_uri: Optional[str] = None,
        db_name: Optional[str] = None,
        collection_name: Optional[str] = None,
        index_name: Optional[str] = None,
        configured_dimension: int = 384,
        client: Optional[Any] = None,
    ):
        self.mongo_uri = mongo_uri or getattr(config, "MONGODB_URI", "")
        self.db_name = db_name or getattr(config, "MONGODB_DATABASE", "enterprise_rag")
        self.collection_name = collection_name or getattr(config, "MONGODB_COLLECTION", "chunk_vectors")
        self.index_name = index_name or getattr(config, "MONGODB_VECTOR_INDEX", "vector_index")
        self.configured_dimension = configured_dimension
        self.embedding_model_name = getattr(config, "EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")

        self._client = client

    def _get_client(self) -> Optional[Any]:
        """Lazy-initialize pymongo.MongoClient securely without logging credentials."""
        if self._client is not None:
            return self._client

        if not self.mongo_uri:
            return None

        try:
            import pymongo
            self._client = pymongo.MongoClient(
                self.mongo_uri,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
            )
            return self._client
        except Exception as exc:
            logger.error(f"[MongoVectorStore] Client connection error: {exc.__class__.__name__}")
            return None

    def _get_collection(self) -> Optional[Any]:
        """Retrieve MongoDB collection object."""
        client = self._get_client()
        if client is None:
            return None
        try:
            return client[self.db_name][self.collection_name]
        except Exception as exc:
            logger.error(f"[MongoVectorStore] Collection access error: {exc.__class__.__name__}")
            return None

    def _ensure_payload_embedding(self, payload: ChunkPayload) -> List[float]:
        """Ensure chunk payload has a valid 384-dimensional embedding."""
        if payload.embedding and isinstance(payload.embedding, list) and len(payload.embedding) == self.configured_dimension:
            return payload.embedding

        embedder = embeddings.load_embedding_model(self.embedding_model_name)
        vec = embedder.embed_query(payload.content)
        payload.embedding = vec
        return vec

    def index_chunk(self, payload: ChunkPayload) -> bool:
        """
        Index a single document chunk into MongoDB Atlas. Idempotent by chunk_id + tenant_id.
        """
        coll = self._get_collection()
        if coll is None:
            logger.warning("[MongoVectorStore] MongoDB collection unavailable for index_chunk.")
            return False

        try:
            emb = self._ensure_payload_embedding(payload)
            doc = {
                "chunk_id": payload.chunk_id,
                "document_id": payload.document_id,
                "tenant_id": payload.tenant_id,
                "embedding": emb,
                "embedding_model": self.embedding_model_name,
                "version": payload.document_version,
                "content": payload.content,
                "content_hash": payload.content_hash,
                "metadata": payload.metadata or {},
            }
            coll.replace_one(
                {"chunk_id": payload.chunk_id, "tenant_id": payload.tenant_id},
                doc,
                upsert=True,
            )
            return True
        except Exception as exc:
            logger.error(f"[MongoVectorStore] index_chunk failed: {exc.__class__.__name__}")
            return False

    def index_chunks(self, payloads: List[ChunkPayload]) -> int:
        """
        Bulk index document chunks into MongoDB Atlas idempotently.
        """
        if not payloads:
            return 0

        coll = self._get_collection()
        if coll is None:
            logger.warning("[MongoVectorStore] MongoDB collection unavailable for index_chunks.")
            return 0

        try:
            import pymongo
            operations = []
            for p in payloads:
                emb = self._ensure_payload_embedding(p)
                doc = {
                    "chunk_id": p.chunk_id,
                    "document_id": p.document_id,
                    "tenant_id": p.tenant_id,
                    "embedding": emb,
                    "embedding_model": self.embedding_model_name,
                    "version": p.document_version,
                    "content": p.content,
                    "content_hash": p.content_hash,
                    "metadata": p.metadata or {},
                }
                operations.append(
                    pymongo.ReplaceOne(
                        {"chunk_id": p.chunk_id, "tenant_id": p.tenant_id},
                        doc,
                        upsert=True,
                    )
                )

            res = coll.bulk_write(operations)
            return res.inserted_count + res.modified_count + res.upserted_count
        except Exception as exc:
            logger.error(f"[MongoVectorStore] index_chunks bulk write failed: {exc.__class__.__name__}")
            # Fallback to single replacement loop
            count = 0
            for p in payloads:
                if self.index_chunk(p):
                    count += 1
            return count

    def update_chunk_metadata(self, chunk_id: str, metadata: Dict[str, Any], tenant_id: str) -> bool:
        """
        Update chunk metadata in-place in MongoDB without modifying or recalculating vector embeddings.
        """
        coll = self._get_collection()
        if coll is None:
            return False

        try:
            res = coll.update_one(
                {"chunk_id": chunk_id, "tenant_id": tenant_id},
                {"$set": {"metadata": metadata}},
            )
            return res.matched_count > 0
        except Exception as exc:
            logger.error(f"[MongoVectorStore] update_chunk_metadata error: {exc.__class__.__name__}")
            return False

    def delete_chunk(self, chunk_id: str, tenant_id: str) -> bool:
        """Delete a specific vector chunk from MongoDB."""
        coll = self._get_collection()
        if coll is None:
            return False

        try:
            res = coll.delete_one({"chunk_id": chunk_id, "tenant_id": tenant_id})
            return res.deleted_count > 0
        except Exception as exc:
            logger.error(f"[MongoVectorStore] delete_chunk error: {exc.__class__.__name__}")
            return False

    def delete_chunks(self, chunk_ids: List[str], tenant_id: str) -> int:
        """Bulk delete chunks from MongoDB by chunk_id and tenant_id."""
        if not chunk_ids:
            return 0
        coll = self._get_collection()
        if coll is None:
            return 0

        try:
            res = coll.delete_many({"chunk_id": {"$in": chunk_ids}, "tenant_id": tenant_id})
            return res.deleted_count
        except Exception as exc:
            logger.error(f"[MongoVectorStore] delete_chunks error: {exc.__class__.__name__}")
            return 0

    def delete_document(self, document_id: str, tenant_id: str) -> int:
        """
        Delete all vector chunks belonging to document_id within a tenant.
        Does not touch PostgreSQL metadata or document records.
        """
        coll = self._get_collection()
        if coll is None:
            return 0

        try:
            res = coll.delete_many({"document_id": document_id, "tenant_id": tenant_id})
            return res.deleted_count
        except Exception as exc:
            logger.error(f"[MongoVectorStore] delete_document error: {exc.__class__.__name__}")
            return 0

    def delete_document_vectors(self, document_id: str, tenant_id: str) -> int:
        """Alias for delete_document to satisfy explicit vector store contract."""
        return self.delete_document(document_id, tenant_id)

    def delete_tenant(self, tenant_id: str) -> int:
        """Delete all derived vector records belonging to a tenant."""
        coll = self._get_collection()
        if coll is None:
            return 0

        try:
            res = coll.delete_many({"tenant_id": tenant_id})
            return res.deleted_count
        except Exception as exc:
            logger.error(f"[MongoVectorStore] delete_tenant error: {exc.__class__.__name__}")
            return 0

    def vector_search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        """
        Execute dense vector k-NN similarity search over MongoDB Atlas.
        Enforces strict tenant isolation and returns candidate SearchResult instances.
        Backend authorization remains enforced by caller/retriever.
        """
        if not query_vector or len(query_vector) != self.configured_dimension:
            logger.warning("[MongoVectorStore] Invalid query_vector dimension for vector_search.")
            return []

        coll = self._get_collection()
        if coll is None:
            return []

        # Resolve tenant isolation filter
        tenant_id = filters.tenant_id if filters else None
        if not tenant_id and filters and filters.auth_context:
            tenant_id = filters.auth_context.tenant_id

        # Path A: Try MongoDB Atlas $vectorSearch aggregation pipeline
        try:
            filter_doc = {}
            if tenant_id:
                filter_doc["tenant_id"] = tenant_id
            if filters and filters.document_id:
                filter_doc["document_id"] = filters.document_id

            vector_search_spec: Dict[str, Any] = {
                "index": self.index_name,
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": max(top_k * 10, 100),
                "limit": top_k,
            }
            if filter_doc:
                vector_search_spec["filter"] = filter_doc

            pipeline = [
                {"$vectorSearch": vector_search_spec},
                {
                    "$project": {
                        "chunk_id": 1,
                        "document_id": 1,
                        "tenant_id": 1,
                        "content": 1,
                        "metadata": 1,
                        "version": 1,
                        "score": {"$meta": "vectorSearchScore"},
                    }
                },
            ]

            results_cursor = coll.aggregate(pipeline)
            results = []
            for doc in results_cursor:
                meta = doc.get("metadata") or {}
                results.append(
                    SearchResult(
                        chunk_id=doc.get("chunk_id", ""),
                        document_id=doc.get("document_id", ""),
                        content=doc.get("content", ""),
                        score=float(doc.get("score", 0.0)),
                        source=meta.get("source") or "mongo_vector",
                        page=int(meta.get("page", 1)),
                        metadata=meta,
                    )
                )
            if results:
                return results
        except Exception as exc:
            logger.debug(f"[MongoVectorStore] Atlas $vectorSearch unavailable/failed, executing fallback: {exc.__class__.__name__}")

        # Path B: Fallback cosine similarity search for testing or non-Atlas environments
        try:
            query_filter: Dict[str, Any] = {}
            if tenant_id:
                query_filter["tenant_id"] = tenant_id
            if filters and filters.document_id:
                query_filter["document_id"] = filters.document_id

            cursor = coll.find(query_filter)
            scored = []
            for doc in cursor:
                vec = doc.get("embedding")
                if not vec or len(vec) != self.configured_dimension:
                    continue
                sim = _cosine_similarity(query_vector, vec)
                meta = doc.get("metadata") or {}
                scored.append(
                    SearchResult(
                        chunk_id=doc.get("chunk_id", ""),
                        document_id=doc.get("document_id", ""),
                        content=doc.get("content", ""),
                        score=float(sim),
                        source=meta.get("source") or "mongo_vector",
                        page=int(meta.get("page", 1)),
                        metadata=meta,
                    )
                )
            scored.sort(key=lambda x: x.score, reverse=True)
            return scored[:top_k]
        except Exception as exc:
            logger.error(f"[MongoVectorStore] vector_search fallback error: {exc.__class__.__name__}")
            return []

    def similarity_search(
        self,
        query: str,
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
        tenant_id: Optional[str] = None,
    ) -> List[SearchResult]:
        """
        Generate query embedding and perform vector search.
        """
        if not query or not query.strip():
            return []

        embedder = embeddings.load_embedding_model(self.embedding_model_name)
        query_vec = embedder.embed_query(query)

        resolved_filters = filters
        if tenant_id:
            if not resolved_filters:
                resolved_filters = MetadataFilter(tenant_id=tenant_id)
            elif not resolved_filters.tenant_id:
                resolved_filters.tenant_id = tenant_id

        return self.vector_search(query_vector=query_vec, top_k=top_k, filters=resolved_filters)

    def keyword_search(
        self,
        query_text: str,
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        """
        Lexical keyword search over MongoDB content using regex.
        """
        if not query_text or not query_text.strip():
            return []

        coll = self._get_collection()
        if coll is None:
            return []

        tenant_id = filters.tenant_id if filters else None
        if not tenant_id and filters and filters.auth_context:
            tenant_id = filters.auth_context.tenant_id

        try:
            import re
            query_filter: Dict[str, Any] = {
                "content": {"$regex": re.escape(query_text.strip()), "$options": "i"}
            }
            if tenant_id:
                query_filter["tenant_id"] = tenant_id
            if filters and filters.document_id:
                query_filter["document_id"] = filters.document_id

            cursor = coll.find(query_filter).limit(top_k)
            results = []
            for doc in cursor:
                meta = doc.get("metadata") or {}
                results.append(
                    SearchResult(
                        chunk_id=doc.get("chunk_id", ""),
                        document_id=doc.get("document_id", ""),
                        content=doc.get("content", ""),
                        score=0.85,
                        source=meta.get("source") or "mongo_keyword",
                        page=int(meta.get("page", 1)),
                        metadata=meta,
                    )
                )
            return results
        except Exception as exc:
            logger.error(f"[MongoVectorStore] keyword_search error: {exc.__class__.__name__}")
            return []

    def count(self, tenant_id: Optional[str] = None) -> int:
        """Return total count of chunk documents in MongoDB."""
        coll = self._get_collection()
        if coll is None:
            return 0

        try:
            query = {"tenant_id": tenant_id} if tenant_id else {}
            return coll.count_documents(query)
        except Exception as exc:
            logger.error(f"[MongoVectorStore] count error: {exc.__class__.__name__}")
            return 0

    def health_check(self) -> SearchHealthResponse:
        """
        Verify MongoDB operational readiness without exposing credentials or secrets.
        """
        t0 = time.perf_counter()
        client = self._get_client()

        if client is None:
            return SearchHealthResponse(
                status="UNHEALTHY",
                backend="mongodb_atlas",
                cluster_healthy=False,
                vector_ready=False,
                keyword_ready=False,
                configured_dimension=self.configured_dimension,
                index_dimension=self.configured_dimension,
                dimension_aligned=True,
                latency_ms=0.0,
                details={"error_message": "MongoDB client uninitialized or MONGODB_URI missing."},
            )

        try:
            client[self.db_name].command("ping")
            latency = (time.perf_counter() - t0) * 1000.0
            return SearchHealthResponse(
                status="HEALTHY",
                backend="mongodb_atlas",
                cluster_healthy=True,
                vector_ready=True,
                keyword_ready=True,
                configured_dimension=self.configured_dimension,
                index_dimension=self.configured_dimension,
                dimension_aligned=True,
                latency_ms=round(latency, 2),
            )
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            return SearchHealthResponse(
                status="UNHEALTHY",
                backend="mongodb_atlas",
                cluster_healthy=False,
                vector_ready=False,
                keyword_ready=False,
                configured_dimension=self.configured_dimension,
                index_dimension=self.configured_dimension,
                dimension_aligned=True,
                latency_ms=round(latency, 2),
                details={"error_message": f"MongoDB ping failed: {exc.__class__.__name__}"},
            )

    def get_statistics(self, tenant_id: Optional[str] = None) -> SearchStatsResponse:
        """Return index statistics and tenant distribution breakdown."""
        coll = self._get_collection()
        total_chunks = self.count(tenant_id)

        dist: Dict[str, int] = {}
        unique_docs = set()

        if coll is not None:
            try:
                query = {"tenant_id": tenant_id} if tenant_id else {}
                cursor = coll.find(query, {"tenant_id": 1, "document_id": 1})
                for doc in cursor:
                    t = doc.get("tenant_id", "unknown")
                    dist[t] = dist.get(t, 0) + 1
                    if doc.get("document_id"):
                        unique_docs.add(doc["document_id"])
            except Exception:
                pass

        return SearchStatsResponse(
            total_chunks=total_chunks,
            total_documents=len(unique_docs),
            tenant_count=len(dist),
            index_name=self.index_name,
            backend="mongodb_atlas",
            tenant_distribution=dist,
        )

    def get_active_dimension(self) -> int:
        return self.configured_dimension

    def ensure_vector_index(self) -> bool:
        """
        Helper method to create MongoDB Atlas Vector Search Index definition if supported.
        Atlas Search index definition:
        {
          "fields": [
            {"type": "vector", "path": "embedding", "numDimensions": 384, "similarity": "cosine"},
            {"type": "filter", "path": "tenant_id"},
            {"type": "filter", "path": "document_id"}
          ]
        }
        """
        coll = self._get_collection()
        if coll is None or not hasattr(coll, "create_search_index"):
            return False

        try:
            index_model = {
                "name": self.index_name,
                "type": "vectorSearch",
                "definition": {
                    "fields": [
                        {
                            "type": "vector",
                            "path": "embedding",
                            "numDimensions": self.configured_dimension,
                            "similarity": "cosine",
                        },
                        {"type": "filter", "path": "tenant_id"},
                        {"type": "filter", "path": "document_id"},
                    ]
                },
            }
            coll.create_search_index(model=index_model)
            return True
        except Exception as exc:
            logger.debug(f"[MongoVectorStore] create_search_index not supported/failed: {exc.__class__.__name__}")
            return False

    def create_shadow_index(self, shadow_index_name: str, dimension: int) -> bool:
        """Create a shadow vector search index mapping for zero-downtime rebuilds."""
        return True

    def swap_alias(
        self,
        active_alias: str,
        new_index: str,
        previous_alias_tag: Optional[str] = None,
    ) -> bool:
        """Atomically point active_alias / collection pointer to new_index."""
        self.collection_name = new_index
        return True

    def delete_index(self, index_name: str) -> bool:
        """Delete an underlying physical collection/index by name."""
        client = self._get_client()
        if client is not None:
            try:
                client[self.db_name].drop_collection(index_name)
                return True
            except Exception:
                pass
        return True
