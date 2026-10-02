from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class SearchResult(BaseModel):
    """
    Standardized internal search result representing a single document chunk.
    """
    chunk_id: str
    document_id: str
    content: str
    score: float
    source: str
    page: int
    metadata: Dict[str, Any] = Field(default_factory=dict)

class RetrievalDiagnostics(BaseModel):
    """
    Diagnostic metrics about the retrieval pipeline execution.
    """
    query_count: int = 0
    vector_candidate_count: int = 0
    bm25_candidate_count: int = 0
    rrf_candidate_count: int = 0
    reranker_candidate_count: int = 0
    final_result_count: int = 0
    retrieval_latency_ms: float = 0.0
    reranking_latency_ms: float = 0.0

class RetrievalResponse(BaseModel):
    """
    Final output returned by the retrieval pipeline.
    """
    results: List[SearchResult]
    diagnostics: RetrievalDiagnostics
