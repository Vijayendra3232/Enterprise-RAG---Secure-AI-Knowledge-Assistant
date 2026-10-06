import os
from abc import ABC, abstractmethod
from typing import List, Tuple, Dict, Any, Optional
from langchain_core.documents import Document
from app.core import embeddings
from app import config
from app.retrieval.models import SearchResult
from app.retrieval.filters import MetadataFilter

class VectorStoreInterface(ABC):
    """
    Abstract Interface for Vector Database storage layer.
    Allows easy swap of backend implementations (Chroma, pgvector, OpenSearch, etc.)
    """
    @abstractmethod
    def similarity_search_with_score(
        self, query: str, k: int, filter: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[Document, float]]:
        """
        Query the vector store and return list of documents along with distance/similarity scores.
        """
        pass


class ChromaVectorStore(VectorStoreInterface):
    """
    Chroma Vector Store implementation used for local development and persistence.
    """
    def __init__(self, persist_dir: str, collection_name: str, embedding_model_name: str):
        import chromadb
        from langchain_community.vectorstores import Chroma

        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self.embedding_model_name = embedding_model_name
        
        self.client = chromadb.PersistentClient(persist_dir)
        
        # Verify collection exists or trigger dev fallback
        colls = [c.name for c in self.client.list_collections()]
        if collection_name not in colls:
            # Development fallback: ingest if data is available
            if os.path.exists(config.DATA_DIR):
                print(f"[VectorStore] Collection '{collection_name}' not found. Triggering dev fallback auto-ingestion...")
                from app.ingestion.pipeline import IngestionPipeline
                pipeline = IngestionPipeline(embedding_model_name)
                pipeline.run(config.DATA_DIR, persist_dir, collection_name)
                
                # Re-check collections after pipeline ingestion run
                colls = [c.name for c in self.client.list_collections()]
                if collection_name not in colls:
                    raise RuntimeError(f"Ingestion ran but collection '{collection_name}' was still not created.")
            else:
                msg = (f"The collection '{collection_name}' does not exist in the supplied directory '{persist_dir}'. "
                       f"Available collections: {colls}. Auto-ingestion data dir '{config.DATA_DIR}' not found.")
                raise RuntimeError(msg)

        self.embedding_model = embeddings.load_embedding_model(embedding_model_name)
        self.vectordb = Chroma(
            client=self.client,
            embedding_function=self.embedding_model,
            collection_name=collection_name
        )
        print(f"Loaded vector store {self.vectordb._collection.name} containing {self.vectordb._collection.count()} entries...")

    def similarity_search_with_score(
        self, query: str, k: int, filter: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[Document, float]]:
        # Chroma uses 'filter' for metadata queries
        return self.vectordb.similarity_search_with_score(query, k, filter=filter)


def get_vector_store(
    persist_dir: str = config.VECTOR_DB_DIR,
    collection_name: str = config.COLLECTION_NAME,
    embedding_model_name: str = config.EMBEDDING_MODEL_NAME
) -> VectorStoreInterface:
    """
    Factory function returning the configured vector database client.
    """
    return ChromaVectorStore(persist_dir, collection_name, embedding_model_name)


class VectorRetriever:
    """
    Standardized retriever that accepts metadata filters and queries OpenSearch or Chroma,
    returning standard SearchResult instances.
    """
    def __init__(self, vector_store: Any, embedding_model_name: Optional[str] = None):
        self.vector_store = vector_store
        self.embedding_model_name = embedding_model_name or config.EMBEDDING_MODEL_NAME
        self._embedding_model = None

    @property
    def embedding_model(self):
        if self._embedding_model is None:
            self._embedding_model = embeddings.load_embedding_model(self.embedding_model_name)
        return self._embedding_model

    def retrieve(self, query: str, top_k: int = 10, filters: Optional[MetadataFilter] = None) -> List[SearchResult]:
        # Path 1: Modern SearchStoreInterface (OpenSearch / Provider-agnostic store)
        if hasattr(self.vector_store, "vector_search"):
            query_vector = self.embedding_model.embed_query(query)
            raw_search_results = self.vector_store.vector_search(
                query_vector=query_vector,
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

        # Path 2: Legacy VectorStoreInterface (Chroma)
        chroma_filter = filters.to_chroma_filter() if filters else None
        
        raw_results = self.vector_store.similarity_search_with_score(
            query=query,
            k=top_k,
            filter=chroma_filter
        )
        
        results = []
        for doc, distance in raw_results:
            # L2 distance conversion: smaller distance -> higher similarity score in [0, 1]
            similarity_score = 1.0 / (1.0 + distance)
            
            # Extract citation identifiers
            meta = doc.metadata or {}
            
            # Defense-in-depth: strict authorization and metadata verification
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
                    content=doc.page_content,
                    score=similarity_score,
                    source=source,
                    page=page,
                    metadata=meta
                )
            )
        return results

