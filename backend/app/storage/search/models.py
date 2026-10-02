"""
models.py — Data Transfer Objects for the Provider-Agnostic Search Infrastructure.
Defines standardized payloads, health response models, stats models, and rebuild result models.
"""

from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field


class ChunkPayload(BaseModel):
    """
    Standardized chunk document structure ingested into or retrieved from the search store.
    """
    chunk_id: str
    document_id: str
    tenant_id: str
    document_version: int = 1
    content: str
    content_hash: str
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SearchHealthResponse(BaseModel):
    """
    Detailed readiness and health diagnostics for the search infrastructure.
    """
    status: str = "HEALTHY"  # "HEALTHY", "DEGRADED", "UNHEALTHY"
    backend: str  # "opensearch", "chroma_dev", "inmemory_dev", "sqlite_fts_dev"
    cluster_healthy: bool = True
    vector_ready: bool = True
    keyword_ready: bool = True
    configured_dimension: int = 384
    index_dimension: int = 384
    dimension_aligned: bool = True
    latency_ms: float = 0.0
    details: Dict[str, Any] = Field(default_factory=dict)


class SearchStatsResponse(BaseModel):
    """
    Index statistics, capacity, and tenant distribution metrics.
    """
    total_chunks: int = 0
    total_documents: int = 0
    tenant_count: int = 0
    index_name: str = ""
    backend: str = ""
    tenant_distribution: Dict[str, int] = Field(default_factory=dict)
    details: Dict[str, Any] = Field(default_factory=dict)


class RebuildResult(BaseModel):
    """
    Execution and validation summary of an index rebuild operation.
    """
    status: str  # "SUCCESS", "FAILED"
    shadow_index_name: str
    active_index_name: str
    retained_rollback_index_name: Optional[str] = None
    chunks_indexed: int = 0
    reconciled_mutations: int = 0
    validation_passed: bool = False
    validation_checks: Dict[str, bool] = Field(default_factory=dict)
    error_message: Optional[str] = None
    duration_ms: float = 0.0
