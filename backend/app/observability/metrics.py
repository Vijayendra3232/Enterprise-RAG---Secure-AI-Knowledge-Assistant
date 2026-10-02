"""
metrics.py — Production metrics registry with in-code label cardinality enforcement and OpenMetrics exposition.
Supports Counter, Histogram, and Gauge metric types with bounded label validation,
preventing raw user queries, document IDs, user IDs, or tenant IDs from causing label explosions.
"""

import threading
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from app import config

# Banned high-cardinality label names
BANNED_LABEL_NAMES = {
    "request_id",
    "trace_id",
    "user_id",
    "tenant_id",
    "document_id",
    "query",
    "prompt",
    "url",
    "exception_message",
    "token",
    "filename",
    "secret",
    "password",
}

DEFAULT_HISTOGRAM_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0
)


def _format_labels(labels: Optional[Dict[str, str]]) -> str:
    if not labels:
        return ""
    sorted_pairs = sorted(labels.items())
    formatted = ",".join(f'{k}="{v}"' for k, v in sorted_pairs)
    return f"{{{formatted}}}"


class CardinalityGuard:
    """
    Validates metric label keys and values against allowed declarations,
    stripping prohibited high-cardinality keys and capping label value cardinality.
    """

    @staticmethod
    def sanitize_labels(
        labels: Optional[Dict[str, Any]],
        allowed_keys: Optional[Set[str]] = None,
        max_val_len: int = 48,
    ) -> Dict[str, str]:
        if not labels:
            return {}

        sanitized: Dict[str, str] = {}
        for k, v in labels.items():
            key_str = str(k).lower().strip()
            # Drop banned keys
            if key_str in BANNED_LABEL_NAMES:
                continue
            if allowed_keys is not None and key_str not in allowed_keys:
                continue

            # Sanitize value to bounded string
            val_str = str(v).replace('"', "").replace("\n", " ").replace("\r", " ").strip()
            if len(val_str) > max_val_len:
                val_str = val_str[:max_val_len]
            sanitized[key_str] = val_str or "unknown"

        return sanitized


@dataclass
class MetricDefinition:
    name: str
    description: str
    metric_type: str  # counter, histogram, gauge
    allowed_labels: Set[str] = field(default_factory=set)


class Counter:
    """Thread-safe monotonic counter with bounded labels."""

    def __init__(self, definition: MetricDefinition):
        self.definition = definition
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, labels: Optional[Dict[str, Any]] = None) -> None:
        if amount < 0:
            return
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get_value(self, labels: Optional[Dict[str, Any]] = None) -> float:
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> List[Tuple[Dict[str, str], float]]:
        with self._lock:
            return [(dict(k), v) for k, v in self._values.items()]


class Gauge:
    """Thread-safe gauge representing an instantaneous value."""

    def __init__(self, definition: MetricDefinition):
        self.definition = definition
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, labels: Optional[Dict[str, Any]] = None) -> None:
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, labels: Optional[Dict[str, Any]] = None) -> None:
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, labels: Optional[Dict[str, Any]] = None) -> None:
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) - amount

    def get_value(self, labels: Optional[Dict[str, Any]] = None) -> float:
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> List[Tuple[Dict[str, str], float]]:
        with self._lock:
            return [(dict(k), v) for k, v in self._values.items()]


class Histogram:
    """Thread-safe histogram tracking duration and value distribution."""

    def __init__(self, definition: MetricDefinition, buckets: Tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS):
        self.definition = definition
        self.buckets = sorted(buckets)
        self._counts: Dict[Tuple[Tuple[str, str], ...], int] = {}
        self._sums: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._bucket_counts: Dict[Tuple[Tuple[str, str], ...], Dict[float, int]] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, labels: Optional[Dict[str, Any]] = None) -> None:
        sanitized = CardinalityGuard.sanitize_labels(labels, self.definition.allowed_labels)
        key = tuple(sorted(sanitized.items()))
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._sums[key] = self._sums.get(key, 0.0) + value

            if key not in self._bucket_counts:
                self._bucket_counts[key] = {b: 0 for b in self.buckets}

            for b in self.buckets:
                if value <= b:
                    self._bucket_counts[key][b] += 1

    def collect(self) -> List[Dict[str, Any]]:
        with self._lock:
            results = []
            for key, count in self._counts.items():
                labels_dict = dict(key)
                results.append({
                    "labels": labels_dict,
                    "count": count,
                    "sum": self._sums.get(key, 0.0),
                    "buckets": dict(self._bucket_counts.get(key, {})),
                })
            return results


class MetricRegistry:
    """
    Central repository for application metrics.
    Registers typed metrics and exposes OpenMetrics Prometheus format.
    """

    def __init__(self):
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def register_counter(self, name: str, description: str, allowed_labels: Set[str]) -> Counter:
        with self._lock:
            if name in self._counters:
                return self._counters[name]
            defn = MetricDefinition(name=name, description=description, metric_type="counter", allowed_labels=allowed_labels)
            counter = Counter(defn)
            self._counters[name] = counter
            return counter

    def register_gauge(self, name: str, description: str, allowed_labels: Set[str]) -> Gauge:
        with self._lock:
            if name in self._gauges:
                return self._gauges[name]
            defn = MetricDefinition(name=name, description=description, metric_type="gauge", allowed_labels=allowed_labels)
            gauge = Gauge(defn)
            self._gauges[name] = gauge
            return gauge

    def register_histogram(
        self,
        name: str,
        description: str,
        allowed_labels: Set[str],
        buckets: Tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Histogram:
        with self._lock:
            if name in self._histograms:
                return self._histograms[name]
            defn = MetricDefinition(name=name, description=description, metric_type="histogram", allowed_labels=allowed_labels)
            histogram = Histogram(defn, buckets=buckets)
            self._histograms[name] = histogram
            return histogram

    def get_metric(self, name: str) -> Optional[Any]:
        with self._lock:
            return self._counters.get(name) or self._gauges.get(name) or self._histograms.get(name)

    def to_openmetrics(self) -> str:
        """Renders all registered metrics to Prometheus/OpenMetrics text format."""
        lines: List[str] = []
        with self._lock:
            # Render Counters
            for name, counter in sorted(self._counters.items()):
                lines.append(f"# HELP {name} {counter.definition.description}")
                lines.append(f"# TYPE {name} counter")
                for labels, val in counter.collect():
                    lines.append(f"{name}{_format_labels(labels)} {val}")

            # Render Gauges
            for name, gauge in sorted(self._gauges.items()):
                lines.append(f"# HELP {name} {gauge.definition.description}")
                lines.append(f"# TYPE {name} gauge")
                for labels, val in gauge.collect():
                    lines.append(f"{name}{_format_labels(labels)} {val}")

            # Render Histograms
            for name, hist in sorted(self._histograms.items()):
                lines.append(f"# HELP {name} {hist.definition.description}")
                lines.append(f"# TYPE {name} histogram")
                for entry in hist.collect():
                    base_labels = entry["labels"]
                    # Buckets
                    for b_val, b_count in sorted(entry["buckets"].items()):
                        lbls = dict(base_labels)
                        lbls["le"] = str(b_val)
                        lines.append(f"{name}_bucket{_format_labels(lbls)} {b_count}")
                    # Inf bucket
                    inf_lbls = dict(base_labels)
                    inf_lbls["le"] = "+Inf"
                    lines.append(f"{name}_bucket{_format_labels(inf_lbls)} {entry['count']}")
                    # Sum and Count
                    lines.append(f"{name}_sum{_format_labels(base_labels)} {round(entry['sum'], 4)}")
                    lines.append(f"{name}_count{_format_labels(base_labels)} {entry['count']}")

        return "\n".join(lines) + "\n"

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# Global Registry Instance
metric_registry = MetricRegistry()


# ─── Initialize Core Application Metrics ─────────────────────────────────────

# API Metrics
http_requests_total = metric_registry.register_counter(
    "http_requests_total",
    "Total count of HTTP requests processed",
    {"method", "status_class", "endpoint_group"},
)
http_request_duration_seconds = metric_registry.register_histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    {"method", "endpoint_group"},
)
http_exceptions_total = metric_registry.register_counter(
    "http_exceptions_total",
    "Total count of uncaught HTTP exceptions",
    {"error_class", "endpoint_group"},
)

# Auth & JWT Metrics
auth_attempts_total = metric_registry.register_counter(
    "auth_attempts_total",
    "Total count of authentication attempts",
    {"auth_method"},
)
auth_success_total = metric_registry.register_counter(
    "auth_success_total",
    "Total count of successful authentications",
    {"auth_method"},
)
auth_failures_total = metric_registry.register_counter(
    "auth_failures_total",
    "Total count of failed authentications",
    {"error_class"},
)
jwt_rejections_total = metric_registry.register_counter(
    "jwt_rejections_total",
    "Total count of rejected JWT tokens",
    {"rejection_reason"},
)

# Authorization Metrics
authz_checks_total = metric_registry.register_counter(
    "authz_checks_total",
    "Total count of authorization checks evaluated",
    {"access_level"},
)
authz_allow_total = metric_registry.register_counter(
    "authz_allow_total",
    "Total count of authorized access grants",
    {"access_level"},
)
authz_denied_total = metric_registry.register_counter(
    "authz_denied_total",
    "Total count of authorization denials",
    {"reason"},
)
cross_tenant_attempts_total = metric_registry.register_counter(
    "cross_tenant_attempts_total",
    "Total count of cross-tenant access attempts denied",
    {"resource_type"},
)
unknown_permissions_total = metric_registry.register_counter(
    "unknown_permissions_total",
    "Total count of unknown permission fail-closed evaluations",
    {"decision"},
)

# Query Intelligence Metrics
query_analysis_total = metric_registry.register_counter(
    "query_analysis_total",
    "Total count of query analysis invocations",
    {"status"},
)
query_rewrite_total = metric_registry.register_counter(
    "query_rewrite_total",
    "Total count of query rewrites",
    {"status"},
)
query_decomposition_total = metric_registry.register_counter(
    "query_decomposition_total",
    "Total count of query decompositions",
    {"status"},
)
query_intelligence_duration_seconds = metric_registry.register_histogram(
    "query_intelligence_duration_seconds",
    "Query intelligence stage duration in seconds",
    {"stage"},
)

# Retrieval Metrics
retrieval_requests_total = metric_registry.register_counter(
    "retrieval_requests_total",
    "Total count of retrieval searches executed",
    {"strategy", "status"},
)
retrieval_duration_seconds = metric_registry.register_histogram(
    "retrieval_duration_seconds",
    "Retrieval stage duration in seconds",
    {"strategy"},
)
retrieval_candidates_count = metric_registry.register_histogram(
    "retrieval_candidates_count",
    "Distribution of retrieved candidate count",
    {"candidate_type"},
    buckets=(0, 1, 5, 10, 20, 50, 100),
)
retrieval_empty_total = metric_registry.register_counter(
    "retrieval_empty_total",
    "Total count of empty retrieval results",
    {"strategy"},
)

# Reranking Metrics
reranking_requests_total = metric_registry.register_counter(
    "reranking_requests_total",
    "Total count of cross-encoder reranking operations",
    {"model", "status"},
)
reranking_duration_seconds = metric_registry.register_histogram(
    "reranking_duration_seconds",
    "Reranking latency in seconds",
    {"model"},
)
reranker_failures_total = metric_registry.register_counter(
    "reranker_failures_total",
    "Total count of reranker failures",
    {"error_class"},
)

# Context Optimization Metrics
context_optimization_total = metric_registry.register_counter(
    "context_optimization_total",
    "Total count of context optimization operations",
    {"status"},
)
context_optimization_duration_seconds = metric_registry.register_histogram(
    "context_optimization_duration_seconds",
    "Context optimization latency in seconds",
    {"operation"},
)

# LLM Generation Metrics
llm_requests_total = metric_registry.register_counter(
    "llm_requests_total",
    "Total count of LLM requests dispatched",
    {"provider", "model", "status"},
)
llm_duration_seconds = metric_registry.register_histogram(
    "llm_duration_seconds",
    "LLM generation latency in seconds",
    {"provider", "model"},
)
llm_errors_total = metric_registry.register_counter(
    "llm_errors_total",
    "Total count of LLM provider errors",
    {"provider", "error_class"},
)
llm_tokens_total = metric_registry.register_counter(
    "llm_tokens_total",
    "Total count of tokens processed by LLM",
    {"provider", "token_type"},
)

# Grounding & Verification Metrics
grounding_checks_total = metric_registry.register_counter(
    "grounding_checks_total",
    "Total count of grounding verification evaluations",
    {"status"},
)
grounding_duration_seconds = metric_registry.register_histogram(
    "grounding_duration_seconds",
    "Grounding verification latency in seconds",
    {"status"},
)
citation_validation_failures_total = metric_registry.register_counter(
    "citation_validation_failures_total",
    "Total count of citation validation failures",
    {"reason"},
)

# Worker & Task Subsystem Metrics
task_created_total = metric_registry.register_counter(
    "task_created_total",
    "Total count of tasks created and enqueued",
    {"task_type"},
)
task_claimed_total = metric_registry.register_counter(
    "task_claimed_total",
    "Total count of tasks claimed by workers",
    {"task_type"},
)
task_completed_total = metric_registry.register_counter(
    "task_completed_total",
    "Total count of tasks successfully completed",
    {"task_type"},
)
task_failed_total = metric_registry.register_counter(
    "task_failed_total",
    "Total count of permanently failed tasks",
    {"task_type", "error_class"},
)
task_retry_total = metric_registry.register_counter(
    "task_retry_total",
    "Total count of task retry scheduling attempts",
    {"task_type"},
)
task_stale_recovered_total = metric_registry.register_counter(
    "task_stale_recovered_total",
    "Total count of stale tasks recovered",
    {"status"},
)
task_duration_seconds = metric_registry.register_histogram(
    "task_duration_seconds",
    "Task processing execution duration in seconds",
    {"task_type"},
)
task_queue_depth_gauge = metric_registry.register_gauge(
    "task_queue_depth_gauge",
    "Current number of pending tasks in queue",
    {"queue_name"},
)
oldest_task_age_seconds_gauge = metric_registry.register_gauge(
    "oldest_task_age_seconds_gauge",
    "Age in seconds of the oldest pending task in queue",
    {"queue_name"},
)

# Connector Metrics
connector_sync_started_total = metric_registry.register_counter(
    "connector_sync_started_total",
    "Total count of connector synchronization runs started",
    {"connector_type", "sync_mode"},
)
connector_sync_completed_total = metric_registry.register_counter(
    "connector_sync_completed_total",
    "Total count of connector synchronization runs completed",
    {"connector_type", "status"},
)
connector_sync_failed_total = metric_registry.register_counter(
    "connector_sync_failed_total",
    "Total count of connector synchronization runs failed",
    {"connector_type", "error_class"},
)
connector_sync_duration_seconds = metric_registry.register_histogram(
    "connector_sync_duration_seconds",
    "Connector sync duration in seconds",
    {"connector_type"},
)
connector_docs_processed_total = metric_registry.register_counter(
    "connector_docs_processed_total",
    "Total count of documents processed by connector sync",
    {"connector_type", "action"},
)
connector_permission_changes_total = metric_registry.register_counter(
    "connector_permission_changes_total",
    "Total count of document permission changes synchronized",
    {"connector_type", "action"},
)
connector_errors_total = metric_registry.register_counter(
    "connector_errors_total",
    "Total count of connector adapter errors classified",
    {"connector_type", "error_class"},
)

# Storage Metrics
storage_operations_total = metric_registry.register_counter(
    "storage_operations_total",
    "Total count of blob storage operations executed",
    {"storage_type", "operation", "status"},
)
storage_duration_seconds = metric_registry.register_histogram(
    "storage_duration_seconds",
    "Storage operation duration in seconds",
    {"storage_type", "operation"},
)
storage_bytes_total = metric_registry.register_counter(
    "storage_bytes_total",
    "Total count of bytes transferred to/from storage",
    {"storage_type", "operation"},
)

# Database & Connection Pool Metrics
db_queries_total = metric_registry.register_counter(
    "db_queries_total",
    "Total count of database operations executed",
    {"operation", "status"},
)
db_query_duration_seconds = metric_registry.register_histogram(
    "db_query_duration_seconds",
    "Database query duration in seconds",
    {"operation"},
)
db_slow_queries_total = metric_registry.register_counter(
    "db_slow_queries_total",
    "Total count of slow database queries exceeding threshold",
    {"operation"},
)
db_pool_checked_out_gauge = metric_registry.register_gauge(
    "db_pool_checked_out_gauge",
    "Number of currently checked-out database connections",
    {"pool_name"},
)
db_pool_available_gauge = metric_registry.register_gauge(
    "db_pool_available_gauge",
    "Number of available database connections in pool",
    {"pool_name"},
)

# OpenSearch & Search Store Metrics
opensearch_requests_total = metric_registry.register_counter(
    "opensearch_requests_total",
    "Total count of OpenSearch operations executed",
    {"operation", "status"},
)
opensearch_duration_seconds = metric_registry.register_histogram(
    "opensearch_duration_seconds",
    "OpenSearch operation latency in seconds",
    {"operation"},
)
opensearch_errors_total = metric_registry.register_counter(
    "opensearch_errors_total",
    "Total count of OpenSearch errors",
    {"operation", "error_class"},
)

# Security Event Metrics
security_events_total = metric_registry.register_counter(
    "security_events_total",
    "Total count of structured security events emitted",
    {"event_type", "result"},
)
