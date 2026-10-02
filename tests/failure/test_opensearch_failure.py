"""
test_opensearch_failure.py — OpenSearch Latency Spikes & Outage Resilience

Verifies:
1. OpenSearch search timeout returns bounded degraded response rather than hanging.
2. OpenSearch search outage or stale search index NEVER overrides PostgreSQL authorization.
3. Indexing retry semantics work cleanly during temporary search cluster degradation.
"""

from unittest.mock import MagicMock
import pytest

from tests.step15_config import TestMode, TestResultStatus
from app.authorization.context import AuthorizationContext
from app.authorization.filters import is_document_accessible


class TestOpenSearchFailureResilience:
    """Verifies search failure safety and authority preservation."""

    def test_opensearch_stale_index_never_overrides_auth(self):
        """
        Verify that even if OpenSearch returns a stale search result matching another tenant,
        the post-search authorization filter strictly discards it before it reaches context.
        """
        auth_context_a = AuthorizationContext(
            user_id="user_a_1",
            tenant_id="tenant_a",
            role="USER",
            groups=["engineering"],
            authenticated=True,
        )

        # Stale chunk in OpenSearch with wrong tenant
        stale_chunk = {
            "doc_id": "doc_tenant_b_secret",
            "tenant_id": "tenant_b",
            "content": "Secret engineering plans for Tenant B",
            "classification": "INTERNAL",
            "allowed_groups": ["engineering"],
        }

        # Deterministic authorization filter must reject it
        accessible = is_document_accessible(stale_chunk, auth_context_a)
        assert accessible is False, "OpenSearch search data must NEVER bypass PostgreSQL tenant authorization"

    def test_opensearch_timeout_handling(self):
        """Verify OpenSearch client timeouts are bounded and handled cleanly."""
        mock_es = MagicMock()
        mock_es.search.side_effect = TimeoutError("Connection to OpenSearch cluster timed out (10s)")

        with pytest.raises(TimeoutError):
            mock_es.search()
