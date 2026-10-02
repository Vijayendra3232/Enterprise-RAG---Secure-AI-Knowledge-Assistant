"""
factory.py — Search Store Factory and Dependency Provider.
Instantiates the configured SearchStoreInterface implementation based on application configuration.
"""

from typing import Optional, Any
from app import config
from app.storage.search.base import SearchStoreInterface
from app.storage.search.opensearch_store import OpenSearchStore
from app.storage.search.dev_adapters import DevelopmentHybridSearchStore

_search_store_instance: Optional[SearchStoreInterface] = None


def get_search_store(
    backend_type: Optional[str] = None,
    vector_db: Optional[Any] = None,
    dimension: Optional[int] = None,
) -> SearchStoreInterface:
    """
    Factory resolving the active SearchStoreInterface singleton or test instance.
    """
    global _search_store_instance
    resolved_type = (backend_type or getattr(config, "SEARCH_STORE_TYPE", "opensearch")).lower().strip()
    resolved_dim = dimension or getattr(config, "EMBEDDING_DIMENSION", 384)

    if resolved_type == "opensearch":
        return OpenSearchStore(
            base_url=getattr(config, "OPENSEARCH_URL", "http://localhost:9200"),
            index_alias=getattr(config, "OPENSEARCH_INDEX_PREFIX", "enterprise_rag_chunks"),
            configured_dimension=resolved_dim,
            timeout_seconds=getattr(config, "OPENSEARCH_TIMEOUT_SECONDS", 10),
        )

    # Fallback to dev/test adapter
    return DevelopmentHybridSearchStore(
        vector_db=vector_db,
        configured_dimension=resolved_dim,
    )


def reset_search_store() -> None:
    """Reset the singleton instance (useful for test isolation)."""
    global _search_store_instance
    _search_store_instance = None
