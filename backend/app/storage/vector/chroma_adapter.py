"""
chroma_adapter.py — Chroma vector store adapter implementation.
"""

from typing import List, Dict, Any, Optional, Tuple
from langchain_core.documents import Document

from app.storage.vector.base import VectorStoreAdapterInterface
from app.retrieval.vector import ChromaVectorStore, VectorStoreInterface


class ChromaVectorStoreAdapter(VectorStoreAdapterInterface):
    """
    Adapter wrapping ChromaVectorStore for unified persistent storage lifecycle management.
    """
    def __init__(self, vector_store: VectorStoreInterface):
        self._vector_store = vector_store

    @property
    def underlying_store(self) -> VectorStoreInterface:
        return self._vector_store

    def add_documents(self, documents: List[Document]) -> List[str]:
        if not documents:
            return []
        if hasattr(self._vector_store, "vectordb"):
            return self._vector_store.vectordb.add_documents(documents)
        return []

    def delete_documents(self, filter_dict: Dict[str, Any]) -> bool:
        if hasattr(self._vector_store, "vectordb"):
            try:
                self._vector_store.vectordb.delete(where=filter_dict)
                return True
            except Exception as e:
                print(f"[ChromaAdapter] Delete warning: {e}")
                return False
        return False

    def similarity_search_with_score(
        self,
        query: str,
        k: int,
        filter: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[Document, float]]:
        return self._vector_store.similarity_search_with_score(query=query, k=k, filter=filter)

    def count(self) -> int:
        if hasattr(self._vector_store, "vectordb") and hasattr(self._vector_store.vectordb, "_collection"):
            return self._vector_store.vectordb._collection.count()
        return 0

    def health_check(self) -> bool:
        try:
            if hasattr(self._vector_store, "client"):
                self._vector_store.client.heartbeat()
                return True
            return True
        except Exception:
            return False
