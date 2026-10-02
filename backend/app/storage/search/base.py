"""
base.py — Abstract Search Store Interface.
Defines the standardized contract for all production and development search backends.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from app.storage.search.models import ChunkPayload, SearchHealthResponse, SearchStatsResponse, RebuildResult
from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter


class SearchStoreInterface(ABC):
    """
    Provider-agnostic interface for document chunk indexing, vector k-NN similarity search,
    BM25 lexical search, metadata synchronization, and zero-downtime index rebuilding.
    """

    @abstractmethod
    def index_chunk(self, payload: ChunkPayload) -> bool:
        """Index a single document chunk into both vector and keyword searchable structures."""
        pass

    @abstractmethod
    def index_chunks(self, payloads: List[ChunkPayload]) -> int:
        """Bulk index multiple document chunks. Returns the count of successfully indexed chunks."""
        pass

    @abstractmethod
    def update_chunk_metadata(self, chunk_id: str, metadata: Dict[str, Any], tenant_id: str) -> bool:
        """
        Update chunk metadata in-place without re-embedding or modifying vector representations.
        """
        pass

    @abstractmethod
    def delete_chunk(self, chunk_id: str, tenant_id: str) -> bool:
        """Delete a specific chunk from the search store."""
        pass

    @abstractmethod
    def delete_chunks(self, chunk_ids: List[str], tenant_id: str) -> int:
        """Bulk delete a list of chunks by ID scoped to a specific tenant."""
        pass

    @abstractmethod
    def delete_document(self, document_id: str, tenant_id: str) -> int:
        """Delete all chunks belonging to a document within a specific tenant."""
        pass

    @abstractmethod
    def delete_tenant(self, tenant_id: str) -> int:
        """Delete all chunks belonging to a tenant."""
        pass

    @abstractmethod
    def vector_search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        """
        Execute dense vector k-NN similarity search with coarse pre-filtering.
        """
        pass

    @abstractmethod
    def keyword_search(
        self,
        query_text: str,
        top_k: int = 10,
        filters: Optional[MetadataFilter] = None,
    ) -> List[SearchResult]:
        """
        Execute BM25 lexical keyword search with coarse pre-filtering.
        """
        pass

    @abstractmethod
    def count(self, tenant_id: Optional[str] = None) -> int:
        """Return the total number of indexed chunks, optionally scoped to a tenant."""
        pass

    @abstractmethod
    def health_check(self) -> SearchHealthResponse:
        """
        Verify search backend operational readiness, cluster state, and embedding dimension alignment.
        """
        pass

    @abstractmethod
    def get_statistics(self, tenant_id: Optional[str] = None) -> SearchStatsResponse:
        """Return index statistics, capacity, and tenant distribution breakdown."""
        pass

    @abstractmethod
    def get_active_dimension(self) -> int:
        """Return the vector embedding dimension configured on the active index mapping."""
        pass

    @abstractmethod
    def create_shadow_index(self, shadow_index_name: str, dimension: int) -> bool:
        """Create a new shadow index with dynamic vector dimension for index rebuilding."""
        pass

    @abstractmethod
    def swap_alias(
        self,
        active_alias: str,
        new_index: str,
        previous_alias_tag: Optional[str] = None,
    ) -> bool:
        """
        Atomically point active_alias to new_index.
        Optionally tags previous index with previous_alias_tag for rollback retention.
        """
        pass

    @abstractmethod
    def delete_index(self, index_name: str) -> bool:
        """Delete an underlying physical index by name."""
        pass
