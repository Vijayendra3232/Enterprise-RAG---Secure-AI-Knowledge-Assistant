"""
schemas.py — Strict, allowlisted telemetry models.
Enforces that only safe, explicitly approved metadata fields participate in telemetry,
strictly rejecting raw queries, document content, chunk text, prompts, LLM responses,
credentials, tokens, JWTs, and raw ACLs via extra="forbid".
"""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, ConfigDict, Field


class SafeTelemetryBase(BaseModel):
    """Base model enforcing strict allowlisted schema validation."""
    model_config = ConfigDict(extra="forbid")


class APITelemetry(SafeTelemetryBase):
    endpoint_group: str = "other"
    method: str
    status_code: int
    status_class: str = "2xx"
    duration_ms: float
    request_id: str = ""
    trace_id: str = ""
    path: Optional[str] = None
    client_ip_masked: Optional[str] = None
    safe_tenant_id: Optional[str] = ""
    safe_user_id: Optional[str] = ""


class AuthTelemetry(SafeTelemetryBase):
    event: str = "auth_event"
    auth_method: str
    error_class: Optional[str] = None
    safe_tenant_id: Optional[str] = ""
    safe_user_id: Optional[str] = ""


class AuthzTelemetry(SafeTelemetryBase):
    event: str = "authz_check"
    decision: str  # ALLOW or DENY
    access_level: Optional[str] = None
    document_access_level: Optional[str] = None
    permission_status: Optional[str] = None
    reason: Optional[str] = None
    duration_ms: Optional[float] = None
    safe_tenant_id: Optional[str] = ""
    safe_user_id: Optional[str] = ""


class QueryTelemetry(SafeTelemetryBase):
    stage: str = "query"  # analysis, rewrite, decomposition
    duration_ms: float
    query_length: Optional[int] = None
    query_word_count: Optional[int] = None
    intent: Optional[str] = None
    decomposed: Optional[bool] = False
    subquery_count: int = 1
    sub_query_count: int = 1
    has_filters: bool = False


class RetrievalTelemetry(SafeTelemetryBase):
    strategy: str
    duration_ms: float
    top_k: Optional[int] = None
    candidate_count: int = 0
    authorized_candidate_count: int = 0
    denied_candidate_count: int = 0
    vector_hit_count: Optional[int] = None
    keyword_hit_count: Optional[int] = None
    is_empty: bool = False


class RerankingTelemetry(SafeTelemetryBase):
    model: str
    input_count: int = 0
    output_count: int = 0
    duration_ms: float = 0.0
    status: str = "success"


class ContextTelemetry(SafeTelemetryBase):
    deduped_count: int = 0
    compressed_count: int = 0
    budget_chunks: int = 0
    duration_ms: float = 0.0


class LLMTelemetry(SafeTelemetryBase):
    provider: str
    model: str
    duration_ms: float = 0.0
    status: str = "success"
    error_class: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    finish_reason: Optional[str] = None


class GroundingTelemetry(SafeTelemetryBase):
    status: str  # SUPPORTED, PARTIALLY_SUPPORTED, UNSUPPORTED, INSUFFICIENT_EVIDENCE
    claims_count: int = 0
    supported_count: int = 0
    unsupported_count: int = 0
    partially_supported_count: int = 0
    duration_ms: float = 0.0


class WorkerTelemetry(SafeTelemetryBase):
    task_type: str
    task_id_hash: str
    attempt: int = 1
    status: str = "success"
    duration_ms: float = 0.0
    retry_delay_s: Optional[int] = None
    error_class: Optional[str] = None


class ConnectorTelemetry(SafeTelemetryBase):
    connector_type: str
    sync_mode: str
    status: str
    discovered_count: int = 0
    added_count: int = 0
    updated_count: int = 0
    deleted_count: int = 0
    permissions_updated_count: int = 0
    duration_ms: float = 0.0
    error_class: Optional[str] = None


class StorageTelemetry(SafeTelemetryBase):
    storage_type: str
    operation: str  # upload, download, delete, checksum, save, get
    duration_ms: float = 0.0
    bytes_transferred: int = 0
    size_bytes: Optional[int] = None
    status: str = "success"


class DatabaseTelemetry(SafeTelemetryBase):
    operation: str
    duration_ms: float = 0.0
    is_slow: bool = False
    pool_checked_out: Optional[int] = None
    pool_available: Optional[int] = None


class OpenSearchTelemetry(SafeTelemetryBase):
    operation: str  # vector_search, keyword_search, index_batch, bulk_index
    duration_ms: float = 0.0
    top_k: Optional[int] = None
    hit_count: Optional[int] = None
    doc_count: int = 0
    status: str = "success"


class SecurityTelemetry(SafeTelemetryBase):
    event_type: str
    severity: str = "INFO"
    action: str = ""
    result: str = ""
    details: Optional[Dict[str, Any]] = None


class LogRecordSchema(SafeTelemetryBase):
    timestamp: str
    level: str
    logger: str = "enterprise-rag"
    service: str = "enterprise-rag-api"
    environment: str = "development"
    event: str
    message: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    safe_tenant_id: Optional[str] = None
    safe_user_id: Optional[str] = None
    duration_ms: Optional[float] = None
    status: Optional[str] = None
    error_class: Optional[str] = None
    error_code: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None
