"""
Search package initialization.
Exports standardized models, interface, production OpenSearch store, and search factory.
"""

from app.storage.search.models import (
    ChunkPayload,
    SearchHealthResponse,
    SearchStatsResponse,
    RebuildResult,
)
from app.storage.search.base import SearchStoreInterface
from app.storage.search.opensearch_store import OpenSearchStore, HttpOpenSearchTransport
from app.storage.search.dev_adapters import DevelopmentHybridSearchStore
from app.storage.search.factory import get_search_store, reset_search_store

__all__ = [
    "ChunkPayload",
    "SearchHealthResponse",
    "SearchStatsResponse",
    "RebuildResult",
    "SearchStoreInterface",
    "OpenSearchStore",
    "HttpOpenSearchTransport",
    "DevelopmentHybridSearchStore",
    "get_search_store",
    "reset_search_store",
]
