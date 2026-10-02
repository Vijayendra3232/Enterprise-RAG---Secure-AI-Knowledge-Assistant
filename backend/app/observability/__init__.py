"""
__init__.py — Unified package exports for production observability.
"""

from app.observability.config import ObservabilityConfig, get_observability_config
from app.observability.context import (
    set_request_context,
    clear_request_context,
    get_request_id,
    get_trace_id,
    get_span_id,
    get_safe_tenant_id,
    get_safe_user_id,
    get_current_context,
    validate_or_generate_request_id,
    generate_trace_id,
    generate_span_id,
)
from app.observability.redaction import (
    SafeIdentityHasher,
    redact_data,
)
from app.observability.errors import (
    ErrorTaxonomy,
    classify_error,
    sanitize_error_detail,
)
from app.observability.schemas import (
    APITelemetry,
    AuthTelemetry,
    AuthzTelemetry,
    QueryTelemetry,
    RetrievalTelemetry,
    RerankingTelemetry,
    ContextTelemetry,
    LLMTelemetry,
    GroundingTelemetry,
    WorkerTelemetry,
    ConnectorTelemetry,
    StorageTelemetry,
    DatabaseTelemetry,
    OpenSearchTelemetry,
    SecurityTelemetry,
    LogRecordSchema,
)
from app.observability.logging import (
    StructuredLogger,
    structured_logger,
)
from app.observability.tracing import (
    Span,
    SpanExporterInterface,
    InMemorySpanExporter,
    AsyncBatchSpanExporter,
    Tracer,
    tracer,
)
from app.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricDefinition,
    MetricRegistry,
    CardinalityGuard,
    metric_registry,
)
from app.observability.events import (
    emit_security_event,
    emit_operational_event,
)
from app.observability.timing import StageTimer
from app.observability.middleware import (
    ObservabilityMiddleware,
    metrics_router,
)

__all__ = [
    "ObservabilityConfig",
    "get_observability_config",
    "set_request_context",
    "clear_request_context",
    "get_request_id",
    "get_trace_id",
    "get_span_id",
    "get_safe_tenant_id",
    "get_safe_user_id",
    "get_current_context",
    "validate_or_generate_request_id",
    "generate_trace_id",
    "generate_span_id",
    "SafeIdentityHasher",
    "redact_data",
    "ErrorTaxonomy",
    "classify_error",
    "sanitize_error_detail",
    "APITelemetry",
    "AuthTelemetry",
    "AuthzTelemetry",
    "QueryTelemetry",
    "RetrievalTelemetry",
    "RerankingTelemetry",
    "ContextTelemetry",
    "LLMTelemetry",
    "GroundingTelemetry",
    "WorkerTelemetry",
    "ConnectorTelemetry",
    "StorageTelemetry",
    "DatabaseTelemetry",
    "OpenSearchTelemetry",
    "SecurityTelemetry",
    "LogRecordSchema",
    "StructuredLogger",
    "structured_logger",
    "Span",
    "SpanExporterInterface",
    "InMemorySpanExporter",
    "AsyncBatchSpanExporter",
    "Tracer",
    "tracer",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricDefinition",
    "MetricRegistry",
    "CardinalityGuard",
    "metric_registry",
    "emit_security_event",
    "emit_operational_event",
    "StageTimer",
    "ObservabilityMiddleware",
    "metrics_router",
]
