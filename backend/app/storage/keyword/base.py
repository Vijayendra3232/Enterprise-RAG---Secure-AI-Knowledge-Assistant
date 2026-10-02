"""
base.py — Abstract keyword search store adapter interface.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple


class KeywordSearchStoreInterface(ABC):
    """
    Adapter interface wrapping keyword/lexical search engines (BM25, OpenSearch, Elasticsearch).
    """
    @abstractmethod
    def index_documents(self, documents: List[Dict[str, Any]]) -> None:
        """Index a list of documents (each containing 'id', 'content', 'metadata')."""
        pass

    @abstractmethod
    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        """Perform BM25/keyword search returning (doc, score) tuples."""
        pass

    @abstractmethod
    def count(self) -> int:
        """Return number of indexed documents in the keyword store."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Verify keyword search engine operational status."""
        pass
