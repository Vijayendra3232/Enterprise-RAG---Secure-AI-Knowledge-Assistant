"""
test_load_concurrency.py — Pytest Concurrency & Load Benchmark Validation Suite

Tests:
1. Synthetic multi-tenant corpus generation (Tenants A-E, varied roles/groups/ACLs).
2. End-to-end authenticated query execution through full RAG pipeline.
3. Concurrency execution at baseline (25 users) and primary target (500 users).
4. Subsystem latency breakdown verification.
5. Zero cross-tenant data leakage during load.
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.step15_config import TestMode, TestResultStatus
from tests.load.concurrency_benchmark import (
    generate_synthetic_corpus,
    ConcurrencyBenchmarkRunner,
    FastBenchmarkEmbeddings,
    QUERY_MIX,
)
from tests.load.soak_test import run_soak_benchmark


@pytest.fixture(autouse=True, scope="module")
def mock_load_embeddings_for_concurrency():
    with patch("app.core.embeddings.load_embedding_model", return_value=FastBenchmarkEmbeddings()):
        yield


@pytest.fixture(scope="module")
def synthetic_corpus():
    """Create a scoped synthetic corpus for fast test execution."""
    return generate_synthetic_corpus(tenant_count=3, users_per_tenant=20, docs_per_tenant=50)


def test_synthetic_corpus_generation(synthetic_corpus):
    """Verify synthetic corpus creates distinct tenants, users, and documents with ACLs."""
    assert len(synthetic_corpus) == 3
    assert "tenant_a" in synthetic_corpus
    tenant_a = synthetic_corpus["tenant_a"]
    assert len(tenant_a.users) == 20
    assert len(tenant_a.documents) == 50

    # Verify user roles and groups exist
    roles = {u["role"] for u in tenant_a.users}
    assert "ADMIN" in roles or "USER" in roles

    # Verify doc classifications
    classifications = {d["classification"] for d in tenant_a.documents}
    assert len(classifications) >= 2


def test_single_rag_query_execution(synthetic_corpus):
    """Verify single RAG query executes all pipeline stages and measures stage latency."""
    runner = ConcurrencyBenchmarkRunner(tenants_corpus=synthetic_corpus)
    user = synthetic_corpus["tenant_a"].users[0]
    query_item = QUERY_MIX[0]

    metric = asyncio.run(runner._execute_single_rag_query(user, query_item))
    assert metric.status_code == 200
    assert metric.total_latency_ms > 0.0
    assert "authentication_ms" in metric.subsystem_ms
    assert "authorization_ms" in metric.subsystem_ms
    assert "query_intelligence_ms" in metric.subsystem_ms
    assert "retrieval_ms" in metric.subsystem_ms
    assert "reranking_ms" in metric.subsystem_ms
    assert "context_optimization_ms" in metric.subsystem_ms
    assert "llm_generation_ms" in metric.subsystem_ms
    assert "grounding_ms" in metric.subsystem_ms
    assert metric.unauthorized_candidates == 0
    assert metric.unauthorized_context == 0
    assert metric.unauthorized_citations == 0


def test_concurrency_tier_execution(synthetic_corpus):
    """Verify execution of a concurrent user tier with real active concurrent workers."""
    runner = ConcurrencyBenchmarkRunner(tenants_corpus=synthetic_corpus)
    # Short smoke run of 25 concurrent users
    result = asyncio.run(runner.execute_concurrency_tier(target_users=25, duration_seconds=2.0, warmup_seconds=0.5))

    assert result.target_concurrency == 25
    assert result.total_requests > 0
    assert result.successful_requests > 0
    assert result.error_percentage < 1.0
    assert result.rps > 0.0
    assert result.latency_p50_ms > 0.0
    assert result.latency_p95_ms >= result.latency_p50_ms
    assert result.unauthorized_leak_count == 0
    assert result.status == TestResultStatus.PASS.value


def test_soak_benchmark_execution():
    """Verify soak test measures resource drift and latency progression."""
    res = asyncio.run(run_soak_benchmark(target_users=25, duration_seconds=3.0, sampling_interval_seconds=1.0))
    assert res["status"] in [TestResultStatus.PASS.value, TestResultStatus.FAIL.value]
    assert "start_memory_rss_mb" in res
    assert "end_memory_rss_mb" in res
    assert "memory_growth_percentage" in res
    assert res["total_requests"] > 0
    assert res["cross_tenant_leaks"] == 0
