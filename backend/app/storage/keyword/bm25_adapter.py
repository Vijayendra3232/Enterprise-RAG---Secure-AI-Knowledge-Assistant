"""
bm25_adapter.py — In-memory rank-bm25 adapter implementation.
"""

from typing import List, Dict, Any, Tuple
from app.storage.keyword.base import KeywordSearchStoreInterface
from app.retrieval.bm25 import InMemoryBM25Index, BM25IndexInterface


class InMemoryBM25Adapter(KeywordSearchStoreInterface):
    """
    Adapter wrapping rank-bm25 for unified index management.
    """
    def __init__(self, index: BM25IndexInterface = None):
        self._index = index or InMemoryBM25Index()

    @property
    def underlying_index(self) -> BM25IndexInterface:
        return self._index

    def index_documents(self, documents: List[Dict[str, Any]]) -> None:
        self._index.build(documents)

    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        return self._index.search(query, top_k)

    def count(self) -> int:
        if hasattr(self._index, "docs"):
            return len(self._index.docs)
        return 0

    def health_check(self) -> bool:
        return True
