"""
base.py — Abstract vector store adapter interface.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
from langchain_core.documents import Document


class VectorStoreAdapterInterface(ABC):
    """
    Adapter interface wrapping vector databases (Chroma, pgvector, OpenSearch, etc.).
    """
    @abstractmethod
    def add_documents(self, documents: List[Document]) -> List[str]:
        """Add langchain Documents to the vector store."""
        pass

    @abstractmethod
    def delete_documents(self, filter_dict: Dict[str, Any]) -> bool:
        """Delete documents matching filter dictionary."""
        pass

    @abstractmethod
    def similarity_search_with_score(
        self,
        query: str,
        k: int,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[Document, float]]:
        """Perform similarity search with distance scores."""
        pass

    @abstractmethod
    def count(self) -> int:
        """Return total number of vector embeddings."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Verify vector store connectivity and operational readiness."""
        pass
