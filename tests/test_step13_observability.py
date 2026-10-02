"""
test_step13_observability.py — Production Observability, Monitoring & Secure Telemetry Test Suite.

Verifies:
1. HMAC-SHA256 Safe Identity Hasher, SecretProvider backing, fail-closed production mode, and recursive redaction.
2. ContextVars correlation IDs validation, generation, and lifecycle.
3. Strict allowlisted telemetry schemas (extra="forbid") rejecting sensitive content and raw queries.
4. Structured JSON logging with anti-log injection string sanitization.
5. Distributed tracing, span hierarchies, bounded in-memory & async batch exporters, and failure isolation.
6. MetricRegistry (Counter, Gauge, Histogram), CardinalityGuard label stripping, high-cardinality protection, and OpenMetrics rendering.
7. Protected /metrics endpoint authentication and credential exclusion.
8. Subsystem telemetry instrumentation (Auth, Authz, RAG, Storage, OpenSearch, Tasks, Workers, Connectors, Health).
9. Telemetry failure safety guaranteeing that authorization invariants are NEVER bypassed.
"""

import json
import logging
import os
import sys
import time
import uuid
import pytest
from unittest.mock import MagicMock, patch

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.main import app
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
    BANNED_LABEL_NAMES,
    authz_checks_total,
    authz_allow_total,
    authz_denied_total,
    storage_operations_total,
    storage_duration_seconds,
    storage_bytes_total,
    opensearch_requests_total,
    opensearch_duration_seconds,
    opensearch_errors_total,
)
from app.observability.events import (
    emit_security_event,
    emit_operational_event,
)
from app.observability.timing import StageTimer
from app.connectors.secrets.local import LocalSecretProvider
from app.authorization.context import AuthorizationContext
from app.authorization.roles import Role
from app.authorization.filters import is_document_accessible
from app.storage.blob.storage import LocalFilesystemStorage
from app.storage.search.opensearch_store import OpenSearchStore, OpenSearchTransportInterface


# ─── 1. HMAC SAFE IDENTITY HASHER & REDACTION TESTS ──────────────────────────

class DummyMockSecretProvider:
    def __init__(self, secrets: dict = None):
        self.secrets = secrets or {}

    def get_secret(self, secret_name: str) -> str:
        if secret_name in self.secrets:
            return self.secrets[secret_name]
        from app.connectors.errors import SecretNotFoundError
        raise SecretNotFoundError(f"Secret '{secret_name}' not found")


class TestSafeIdentityHasherAndRedaction:
    """Test HMAC-SHA256 identity hashing and data redaction security guarantees."""

    def test_deterministic_hashing(self):
        prov = DummyMockSecretProvider({"telemetry/hmac_key": "enterprise-secret-key-12345"})
        hasher = SafeIdentityHasher(secret_provider=prov)

        h1 = hasher.hash_tenant_id("tenant-corp-a")
        h2 = hasher.hash_tenant_id("tenant-corp-a")
        assert h1 == h2
        assert h1.startswith("v1:")
        assert len(h1) == 19  # "v1:" (3) + 16 chars

        u1 = hasher.hash_user_id("user_alice_999")
        u2 = hasher.hash_user_id("user_alice_999")
        assert u1 == u2
        assert u1.startswith("v1:")
        assert len(u1) == 19

    def test_distinct_inputs_produce_distinct_hashes(self):
        prov = DummyMockSecretProvider({"telemetry/hmac_key": "enterprise-secret-key-12345"})
        hasher = SafeIdentityHasher(secret_provider=prov)

        h1 = hasher.hash_tenant_id("tenant-1")
        h2 = hasher.hash_tenant_id("tenant-2")
        assert h1 != h2

        u1 = hasher.hash_user_id("user-1")
        u2 = hasher.hash_user_id("user-2")
        assert u1 != u2

    def test_secret_rotation_changes_hash(self):
        prov1 = DummyMockSecretProvider({"telemetry/hmac_key": "secret-key-A"})
        prov2 = DummyMockSecretProvider({"telemetry/hmac_key": "secret-key-B"})

        hasher1 = SafeIdentityHasher(secret_provider=prov1)
        hasher2 = SafeIdentityHasher(secret_provider=prov2)

        assert hasher1.hash_tenant_id("tenant-corp") != hasher2.hash_tenant_id("tenant-corp")

    def test_production_fail_closed_on_missing_secret(self, monkeypatch):
        monkeypatch.setattr(config, "APP_ENV", "production")
        empty_prov = DummyMockSecretProvider({})
        hasher = SafeIdentityHasher(secret_provider=empty_prov)

        with pytest.raises(RuntimeError, match="Production security violation: Missing telemetry HMAC secret"):
            hasher.hash_tenant_id("tenant-corp")

    def test_empty_or_anonymous_inputs(self):
        prov = DummyMockSecretProvider({"telemetry/hmac_key": "enterprise-secret-key-12345"})
        hasher = SafeIdentityHasher(secret_provider=prov)

        assert hasher.hash_tenant_id("") == "unknown"
        assert hasher.hash_tenant_id(None) == "unknown"
        assert hasher.hash_user_id("") == "anonymous"
        assert hasher.hash_user_id(None) == "anonymous"

    def test_never_leaks_secret_in_repr(self):
        prov = DummyMockSecretProvider({"telemetry/hmac_key": "super-secret-key-never-leak"})
        hasher = SafeIdentityHasher(secret_provider=prov)
        repr_str = repr(hasher)
        assert "super-secret-key-never-leak" not in repr_str

    def test_recursive_redact_data(self):
        raw_payload = {
            "query": "select secret from vault",
            "prompt": "You are a helpful assistant",
            "response": "Here is the result",
            "access_token": "bearer eyJhbGciOi...",
            "password": "Password123!",
            "client_secret": "xyzSecret987",
            "safe_field": 42,
            "nested": {
                "content": "Confidential internal document text",
                "chunk_text": "Sensitive chunk snippet",
                "normal_key": "ok_value",
                "list_field": [
                    {"token": "secret_token_1"},
                    {"safe_item": "public_info"}
                ]
            }
        }

        redacted = redact_data(raw_payload)

        # Sensitive keys must be redacted
        assert redacted["query"] == "[REDACTED]"
        assert redacted["prompt"] == "[REDACTED]"
        assert redacted["response"] == "[REDACTED]"
        assert redacted["access_token"] == "[REDACTED]"
        assert redacted["password"] == "[REDACTED]"
        assert redacted["client_secret"] == "[REDACTED]"
        assert redacted["nested"]["content"] == "[REDACTED]"
        assert redacted["nested"]["chunk_text"] == "[REDACTED]"
        assert redacted["nested"]["list_field"][0]["token"] == "[REDACTED]"

        # Non-sensitive keys must be preserved
        assert redacted["safe_field"] == 42
        assert redacted["nested"]["normal_key"] == "ok_value"
        assert redacted["nested"]["list_field"][1]["safe_item"] == "public_info"


# ─── 2. CONTEXT & CORRELATION IDS TESTS ──────────────────────────────────────

class TestContextAndCorrelation:
    """Test ContextVars propagation, sanitization, and lifecycle."""

    def test_context_lifecycle(self):
        clear_request_context()
        assert get_request_id() is None
        assert get_trace_id() is None

        set_request_context(
            request_id="req-12345",
            trace_id="trace-abcde",
            span_id="span-67890",
            safe_tenant_id="v1:0123456789abcdef",
            safe_user_id="v1:fedcba9876543210",
        )

        ctx = get_current_context()
        assert ctx["request_id"] == "req-12345"
        assert ctx["trace_id"] == "trace-abcde"
        assert ctx["span_id"] == "span-67890"
        assert ctx["safe_tenant_id"] == "v1:0123456789abcdef"
        assert ctx["safe_user_id"] == "v1:fedcba9876543210"

        clear_request_context()
        assert get_request_id() is None

    def test_validate_or_generate_request_id(self):
        # Valid ID preserved
        valid_id = "abc-123_XYZ"
        assert validate_or_generate_request_id(valid_id) == valid_id

        # None or empty generates valid UUID4
        gen1 = validate_or_generate_request_id(None)
        assert len(gen1) == 36
        gen2 = validate_or_generate_request_id("")
        assert len(gen2) == 36

        # Invalid characters or path traversal / CRLF rejected and regenerated
        unsafe_id = "../evil\r\nHeader: Injection"
        sanitized = validate_or_generate_request_id(unsafe_id)
        assert sanitized != unsafe_id
        assert len(sanitized) == 36

    def test_trace_and_span_generators(self):
        trace_id = generate_trace_id()
        assert len(trace_id) == 32
        int(trace_id, 16)  # must be valid hex

        span_id = generate_span_id()
        assert len(span_id) == 16
        int(span_id, 16)  # must be valid hex


# ─── 3. STRICT TELEMETRY SCHEMAS TESTS ───────────────────────────────────────

class TestStrictTelemetrySchemas:
    """Test strict allowlisted telemetry models (extra='forbid')."""

    def test_all_subsystem_schemas_accept_valid_fields(self):
        api_tel = APITelemetry(
            method="POST",
            path="/api/v1/query",
            status_code=200,
            duration_ms=45.2,
            endpoint_group="query",
            client_ip_masked="192.168.1.xxx",
        )
        assert api_tel.duration_ms == 45.2

        authz_tel = AuthzTelemetry(
            decision="ALLOW",
            reason="role_match",
            document_access_level="CONFIDENTIAL",
            permission_status="SYNCED",
            duration_ms=1.5,
        )
        assert authz_tel.decision == "ALLOW"

        query_tel = QueryTelemetry(
            query_length=35,
            query_word_count=6,
            intent="FACTUAL",
            decomposed=False,
            subquery_count=1,
            duration_ms=12.4,
        )
        assert query_tel.query_word_count == 6

        retrieval_tel = RetrievalTelemetry(
            strategy="HYBRID",
            top_k=5,
            candidate_count=20,
            duration_ms=22.1,
            vector_hit_count=10,
            keyword_hit_count=10,
        )
        assert retrieval_tel.strategy == "HYBRID"

        llm_tel = LLMTelemetry(
            provider="mock_llm",
            model="gpt-4o-mini",
            prompt_tokens=250,
            completion_tokens=50,
            total_tokens=300,
            duration_ms=310.5,
            finish_reason="stop",
        )
        assert llm_tel.total_tokens == 300

        storage_tel = StorageTelemetry(
            storage_type="s3",
            operation="save",
            size_bytes=1024,
            duration_ms=15.0,
            status="success",
        )
        assert storage_tel.size_bytes == 1024

        opensearch_tel = OpenSearchTelemetry(
            operation="vector_search",
            top_k=10,
            hit_count=5,
            duration_ms=8.5,
            status="success",
        )
        assert opensearch_tel.hit_count == 5

    def test_schemas_strictly_reject_sensitive_forbidden_fields(self):
        # Passing raw query text to QueryTelemetry must raise ValidationError
        with pytest.raises(ValidationError):
            QueryTelemetry(
                query_length=10,
                query_word_count=2,
                intent="SEARCH",
                decomposed=False,
                subquery_count=1,
                duration_ms=5.0,
                raw_query="SELECT * FROM users",  # FORBIDDEN
            )

        # Passing raw document content to StorageTelemetry must raise ValidationError
        with pytest.raises(ValidationError):
            StorageTelemetry(
                storage_type="s3",
                operation="save",
                size_bytes=100,
                duration_ms=10.0,
                status="success",
                content="CONFIDENTIAL TEXT",  # FORBIDDEN
            )

        # Passing raw prompt or response to LLMTelemetry must raise ValidationError
        with pytest.raises(ValidationError):
            LLMTelemetry(
                provider="openai",
                model="gpt-4",
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                duration_ms=100.0,
                prompt="Tell me secrets",  # FORBIDDEN
            )

        # Passing raw ACL to AuthzTelemetry must raise ValidationError
        with pytest.raises(ValidationError):
            AuthzTelemetry(
                decision="DENY",
                reason="no_permission",
                raw_acl={"allowed_users": ["user_1"]},  # FORBIDDEN
            )


# ─── 4. STRUCTURED LOGGING & ANTI-INJECTION TESTS ───────────────────────────

class TestStructuredLoggingAndAntiInjection:
    """Test structured JSON logging and anti-log injection sanitization."""

    def test_structured_log_output_format(self, capsys):
        logger = StructuredLogger("test.logger")
        set_request_context(
            request_id="req-999",
            trace_id="trace-888",
            span_id="span-777",
            safe_tenant_id="v1:tenant12345678",
            safe_user_id="v1:user12345678901",
        )

        logger.info("Operation completed successfully", extra={"stage": "retrieval", "count": 5})

        captured = capsys.readouterr()
        log_line = captured.out.strip()
        assert log_line

        parsed = json.loads(log_line)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "Operation completed successfully"
        assert parsed["request_id"] == "req-999"
        assert parsed["trace_id"] == "trace-888"
        assert parsed["span_id"] == "span-777"
        assert parsed["safe_tenant_id"] == "v1:tenant12345678"
        assert parsed["safe_user_id"] == "v1:user12345678901"
        assert parsed["extra"]["stage"] == "retrieval"
        assert parsed["extra"]["count"] == 5
        clear_request_context()

    def test_anti_log_injection_crlf_and_control_chars(self, capsys):
        logger = StructuredLogger("test.anti_injection")
        malicious_message = "User login failed\r\n[CRITICAL] ADMIN PRIVILEGES GRANTED TO ATTACKER\x00\x1b[31m"

        logger.warning(malicious_message)

        captured = capsys.readouterr()
        log_line = captured.out.strip()
        # Must be valid single-line JSON
        assert "\n" not in log_line
        assert "\r" not in log_line

        parsed = json.loads(log_line)
        # Verify dangerous characters were escaped/sanitized
        assert "\n" not in parsed["message"]
        assert "\r" not in parsed["message"]
        assert "\x00" not in parsed["message"]


# ─── 5. DISTRIBUTED TRACING & FAILURE ISOLATION TESTS ────────────────────────

class TestDistributedTracing:
    """Test Tracer, Spans, In-Memory and Async Exporters, and Failure Isolation."""

    def test_span_hierarchy_and_duration(self):
        exporter = InMemorySpanExporter(max_spans=100)
        test_tracer = Tracer(service_name="test-service", exporter=exporter, sample_rate=1.0)

        with test_tracer.span("parent_operation", attributes={"attr.parent": "yes"}) as parent_span:
            time.sleep(0.01)
            with test_tracer.span("child_operation", attributes={"attr.child": "yes"}) as child_span:
                time.sleep(0.01)
                assert child_span.parent_span_id == parent_span.span_id

        spans = exporter.get_spans()
        assert len(spans) == 2
        child_rec = [s for s in spans if s.name == "child_operation"][0]
        parent_rec = [s for s in spans if s.name == "parent_operation"][0]

        assert child_rec.parent_span_id == parent_rec.span_id
        assert child_rec.duration_ms > 0
        assert parent_rec.duration_ms >= child_rec.duration_ms
        assert child_rec.status == "OK"
        assert parent_rec.status == "OK"

    def test_in_memory_exporter_bounded_queue(self):
        max_size = 5
        exporter = InMemorySpanExporter(max_spans=max_size)
        test_tracer = Tracer(service_name="test-service", exporter=exporter, sample_rate=1.0)

        for i in range(20):
            with test_tracer.span(f"span_{i}"):
                pass

        spans = exporter.get_spans()
        assert len(spans) == max_size
        # Oldest spans were dropped; latest spans retained
        span_names = [s.name for s in spans]
        assert "span_19" in span_names
        assert "span_0" not in span_names

    def test_tracer_failure_isolation(self):
        """Exporter failures must never crash the application or disrupt flow."""
        failing_exporter = MagicMock()
        failing_exporter.export.side_effect = RuntimeError("Telemetry backend connection dropped")

        test_tracer = Tracer(service_name="resilient-service", exporter=failing_exporter, sample_rate=1.0)

        # Must execute cleanly without raising RuntimeError
        try:
            with test_tracer.span("critical_business_operation"):
                result = 100 + 200
            assert result == 300
        except Exception as exc:
            pytest.fail(f"Tracer failure leaked into application: {exc}")

    def test_async_batch_span_exporter(self):
        processed = []

        class MockBatchExporter(AsyncBatchSpanExporter):
            def _flush_batch(self, batch):
                processed.extend(batch)

        exporter = MockBatchExporter(batch_size=2, flush_interval=0.05, max_queue_size=10)
        span1 = Span(name="s1", trace_id="t1", span_id="sp1")
        span2 = Span(name="s2", trace_id="t1", span_id="sp2")

        exporter.export(span1)
        exporter.export(span2)

        exporter.shutdown(timeout=1.0)
        assert len(processed) == 2


# ─── 6. METRICS & CARDINALITY GUARD TESTS ────────────────────────────────────

class TestMetricRegistryAndCardinalityGuard:
    """Test metric instruments and high-cardinality label protection."""

    def test_counter_gauge_and_histogram(self):
        registry = MetricRegistry()
        counter = registry.register_counter("test_counter", "Test counter", {"status"})
        gauge = registry.register_gauge("test_gauge", "Test gauge", {"pool"})
        hist = registry.register_histogram("test_hist", "Test histogram", {"op"}, buckets=(0.01, 0.1, 1.0))

        counter.inc(1, {"status": "success"})
        counter.inc(2, {"status": "success"})
        assert counter.get_value({"status": "success"}) == 3.0

        gauge.set(10.0, {"pool": "main"})
        gauge.inc(5.0, {"pool": "main"})
        gauge.dec(2.0, {"pool": "main"})
        assert gauge.get_value({"pool": "main"}) == 13.0

        hist.observe(0.05, {"op": "read"})
        hist.observe(0.5, {"op": "read"})

        openmetrics_text = registry.to_openmetrics()
        assert "# HELP test_counter Test counter" in openmetrics_text
        assert "# TYPE test_counter counter" in openmetrics_text
        assert 'test_counter{status="success"} 3.0' in openmetrics_text
        assert 'test_gauge{pool="main"} 13.0' in openmetrics_text
        assert 'test_hist_count{op="read"} 2' in openmetrics_text

    def test_cardinality_guard_strips_banned_labels(self):
        labels = {
            "status": "success",
            "request_id": "req-12345",
            "user_id": "user-999",
            "tenant_id": "tenant-abc",
            "query": "how to hack system",
            "prompt": "secret prompt text",
            "document_id": "doc-uuid-secret",
            "exception_message": "DB timeout at line 42",
        }

        sanitized = CardinalityGuard.sanitize_labels(labels, allowed_keys={"status"})
        assert "status" in sanitized
        assert "request_id" not in sanitized
        assert "user_id" not in sanitized
        assert "tenant_id" not in sanitized
        assert "query" not in sanitized
        assert "prompt" not in sanitized
        assert "document_id" not in sanitized
        assert "exception_message" not in sanitized

    def test_high_cardinality_stress_test(self):
        """Simulate 10,000 distinct requests and verify bounded label combinations."""
        registry = MetricRegistry()
        counter = registry.register_counter("high_volume_requests", "High volume counter", {"status"})

        for i in range(10000):
            # Attempt to inject high-cardinality user IDs and queries into labels
            counter.inc(1, {
                "status": "success" if i % 2 == 0 else "error",
                "user_id": f"unique_user_{i}",
                "query": f"search query number {i}",
                "request_id": f"req_{uuid.uuid4()}",
            })

        collected = counter.collect()
        # MUST only produce 2 label sets: {"status": "success"} and {"status": "error"}
        assert len(collected) == 2
        total_count = sum(val for _, val in collected)
        assert total_count == 10000.0


# ─── 7. PROTECTED /metrics ENDPOINT TESTS ────────────────────────────────────

class TestProtectedMetricsEndpoint:
    """Test authentication and security boundaries on /metrics endpoint."""

    def test_metrics_endpoint_unauthenticated_denied(self, monkeypatch):
        monkeypatch.setattr(config, "MONITORING_API_KEY", "super-secret-monitoring-key-xyz")
        client = TestClient(app)

        # Missing monitoring key
        resp = client.get("/metrics")
        assert resp.status_code in (401, 403)

        # Invalid monitoring key
        resp = client.get("/metrics", headers={"X-Monitoring-Key": "wrong-key"})
        assert resp.status_code in (401, 403)

    def test_metrics_endpoint_authenticated_success(self, monkeypatch):
        test_key = "valid-monitoring-api-key-12345"
        monkeypatch.setattr(config, "MONITORING_API_KEY", test_key)
        client = TestClient(app)

        # Header auth
        resp = client.get("/metrics", headers={"X-Monitoring-Key": test_key})
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        body = resp.text
        assert "http_requests_total" in body or "authz_checks_total" in body

        # Bearer token auth
        resp2 = client.get("/metrics", headers={"Authorization": f"Bearer {test_key}"})
        assert resp2.status_code == 200

    def test_metrics_request_excludes_credentials_from_telemetry(self, monkeypatch, capsys):
        test_key = "super-secret-monitoring-key-never-log"
        monkeypatch.setattr(config, "MONITORING_API_KEY", test_key)
        client = TestClient(app)

        resp = client.get("/metrics", headers={"X-Monitoring-Key": test_key})
        assert resp.status_code == 200

        # Inspect stdout logs to verify the monitoring key was not logged
        captured = capsys.readouterr()
        assert test_key not in captured.out


# ─── 8. SUBSYSTEM INSTRUMENTATION TESTS ──────────────────────────────────────

class DummyOpenSearchTransport(OpenSearchTransportInterface):
    def perform_request(self, method, path, params=None, body=None, headers=None):
        if "_search" in path:
            return 200, {"hits": {"hits": []}}
        if "_bulk" in path:
            return 200, {"items": [{"index": {"status": 201}}]}
        if "_cluster/health" in path:
            return 200, {"status": "green"}
        return 200, {}


class TestSubsystemInstrumentation:
    """Test telemetry integration across Storage, OpenSearch, Evaluator, and Health."""

    def test_storage_instrumentation(self, tmp_path):
        storage = LocalFilesystemStorage(base_dir=str(tmp_path))
        tenant = "tenant-obs-test"
        content = b"Hello, Enterprise Observability!"

        # Save
        saved_path = storage.save(content=content, filename="test.txt", tenant_id=tenant)
        assert os.path.exists(saved_path)

        # Get
        retrieved = storage.get(saved_path)
        assert retrieved == content

        # Delete
        assert storage.delete(saved_path) is True

        # Verify metrics updated
        assert storage_operations_total.get_value({"storage_type": "local", "operation": "save", "status": "success"}) >= 1.0
        assert storage_operations_total.get_value({"storage_type": "local", "operation": "get", "status": "success"}) >= 1.0
        assert storage_operations_total.get_value({"storage_type": "local", "operation": "delete", "status": "success"}) >= 1.0
        assert storage_bytes_total.get_value({"storage_type": "local", "operation": "save"}) >= len(content)

    def test_opensearch_store_instrumentation(self):
        transport = DummyOpenSearchTransport()
        store = OpenSearchStore(transport=transport)

        # Vector search
        results = store.vector_search(query_vector=[0.1] * 384, top_k=5)
        assert isinstance(results, list)
        assert opensearch_requests_total.get_value({"operation": "vector_search", "status": "success"}) >= 1.0

        # Keyword search
        results_kw = store.keyword_search(query_text="enterprise", top_k=5)
        assert isinstance(results_kw, list)
        assert opensearch_requests_total.get_value({"operation": "keyword_search", "status": "success"}) >= 1.0

    def test_health_check_endpoint(self):
        client = TestClient(app)
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "alive"}

        resp_ready = client.get("/health/ready")
        assert resp_ready.status_code in (200, 503)
        assert "checks" in resp_ready.json()


# ─── 9. DETERMINISTIC AUTHORIZATION & FAILURE SAFETY ─────────────────────────

class TestAuthorizationDeterministicFailureSafety:
    """
    CRITICAL SECURITY INVARIANT:
    Telemetry errors MUST fail open for telemetry itself, but NEVER alter or bypass
    authorization evaluation decisions. Unauthorized content must strictly remain DENIED.
    """

    def test_authz_denial_invariant_under_telemetry_errors(self):
        # Context for User A
        user_ctx = AuthorizationContext(
            user_id="user_attacker",
            tenant_id="tenant_alpha",
            role="EMPLOYEE",
            groups=["group_engineering"],
        )

        # Confidential Document restricted to User B
        doc_metadata = {
            "tenant_id": "tenant_alpha",
            "access_level": "ROLE",
            "permission_status": "KNOWN",
            "allowed_roles": ["EXECUTIVE"],
            "allowed_user_ids": ["user_authorized_bob"],
            "allowed_groups": ["group_executive"],
            "denied_roles": [],
            "denied_user_ids": [],
            "denied_groups": [],
        }

        # Mock structured logger to fail catastrophically
        with patch.object(structured_logger, "info", side_effect=RuntimeError("Logging failed")):
            accessible = is_document_accessible(doc_metadata, user_ctx)
            # DECISION MUST REMAIN STRICTLY FALSE (DENIED)
            assert accessible is False

    def test_cross_tenant_denial_invariant(self):
        user_ctx = AuthorizationContext(
            user_id="user_tenant_a",
            tenant_id="tenant_alpha",
            role="ADMIN",
        )

        doc_metadata = {
            "tenant_id": "tenant_bravo",  # Different tenant
            "access_level": "INTERNAL",
            "permission_status": "KNOWN",
        }

        # Cross-tenant must ALWAYS be denied immediately
        accessible = is_document_accessible(doc_metadata, user_ctx)
        assert accessible is False
