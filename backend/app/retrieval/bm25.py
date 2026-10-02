import re
from abc import ABC, abstractmethod
from typing import List, Tuple, Dict, Any, Optional
from rank_bm25 import BM25Okapi

from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter
from app.retrieval.vector import VectorStoreInterface

def clean_tokenize(text: str) -> List[str]:
    """Simple alphanumeric lowercase tokenizer for search queries and corpus texts."""
    return re.findall(r'\b\w+\b', text.lower())


class BM25IndexInterface(ABC):
    """
    Abstract interface for the BM25 text indexer.
    Allows swapping the local in-memory rank-bm25 index with a remote search service.
    """
    @abstractmethod
    def build(self, documents: List[Dict[str, Any]]) -> None:
        """Build the index from a list of dicts with keys 'content' and 'metadata'."""
        pass

    @abstractmethod
    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        """Query the index and return list of (document_dict, score) tuples."""
        pass


class InMemoryBM25Index(BM25IndexInterface):
    """
    In-memory BM25 index using rank-bm25.
    """
    def __init__(self):
        self.bm25 = None
        self.docs: List[Dict[str, Any]] = []

    def build(self, documents: List[Dict[str, Any]]) -> None:
        self.docs = documents
        if not documents:
            self.bm25 = None
            return
        
        tokenized_corpus = [clean_tokenize(doc["content"]) for doc in documents]
        self.bm25 = BM25Okapi(tokenized_corpus)

    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        if not self.bm25 or not self.docs:
            return []
        
        tokenized_query = clean_tokenize(query)
        scores = self.bm25.get_scores(tokenized_query)
        
        doc_scores = list(zip(self.docs, scores))
        # Sort by score descending
        doc_scores.sort(key=lambda x: x[1], reverse=True)
        return doc_scores[:top_k]


class BM25Retriever:
    """
    Retrieves documents using BM25 keyword matching via OpenSearch or in-memory fallback.
    """
    def __init__(
        self,
        vector_store: Any = None,
        index: Optional[BM25IndexInterface] = None,
        search_store: Any = None,
    ):
        self.vector_store = vector_store
        self.search_store = search_store or (vector_store if hasattr(vector_store, "keyword_search") else None)
        self.index = index or InMemoryBM25Index()
        if not self.search_store:
            self.rebuild()

    def rebuild(self) -> None:
        """Fetch all documents currently stored in Chroma and rebuild the in-memory index."""
        if self.search_store:
            # Persistent search store handles indexing and rebuilds via IndexRebuilderService
            return

        try:
            if not self.vector_store or not hasattr(self.vector_store, "vectordb"):
                self.index.build([])
                return
            db_data = self.vector_store.vectordb.get()
            documents = []
            ids = db_data.get("ids", [])
            contents = db_data.get("documents", [])
            metadatas = db_data.get("metadatas", []) or [{}] * len(ids)

            for idx, content in enumerate(contents):
                meta = metadatas[idx] if idx < len(metadatas) else {}
                documents.append({
                    "id": ids[idx],
                    "content": content,
                    "metadata": meta
                })

            self.index.build(documents)
            print(f"[BM25Retriever] Rebuilt index with {len(documents)} documents.")
        except Exception as e:
            print(f"[BM25Retriever] Warning: failed to rebuild index: {e}")
            self.index.build([])

    def retrieve(self, query: str, top_k: int = 10, filters: Optional[MetadataFilter] = None) -> List[SearchResult]:
        # Path 1: Modern SearchStoreInterface (OpenSearch / Provider-agnostic store)
        if self.search_store and hasattr(self.search_store, "keyword_search"):
            raw_search_results = self.search_store.keyword_search(
                query_text=query,
                top_k=top_k,
                filters=filters,
            )
            # Defense-in-depth: strict backend authorization and metadata verification
            results: List[SearchResult] = []
            for r in raw_search_results:
                meta = r.metadata or {}
                if filters and not filters.matches(meta):
                    continue
                results.append(r)
            return results

        # Path 2: In-memory rank-bm25 fallback
        if not self.index.docs:
            return []

        # Get scores for the entire corpus to allow post-filtering
        all_scored = self.index.search(query, len(self.index.docs))
        
        results = []
        for doc, score in all_scored:
            if float(score) <= 0.0:
                continue
            meta = doc.get("metadata") or {}
            
            # Apply metadata filters
            if filters and not filters.matches(meta):
                continue
                
            chunk_id = meta.get("chunk_id") or "unknown"
            document_id = meta.get("document_id") or "unknown"
            source = meta.get("file_path") or meta.get("source") or "unknown"
            page = int(meta.get("page") or 1)

            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    content=doc["content"],
                    score=float(score),
                    source=source,
                    page=page,
                    metadata=meta
                )
            )
            if len(results) >= top_k:
                break

        return results

