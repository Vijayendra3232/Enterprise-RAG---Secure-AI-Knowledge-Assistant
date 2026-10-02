"""
test_opensearch_integration.py — Integration Test Suite for Real OpenSearch Clusters.
Executes against a live OpenSearch cluster (default http://localhost:9200).
Skips gracefully if no live OpenSearch cluster is running.
"""

import os
import urllib.request
import pytest
from app.storage.search.opensearch_store import OpenSearchStore, HttpOpenSearchTransport
from app.storage.search.models import ChunkPayload
from app.retrieval.filters import MetadataFilter
from app.authorization import Role, AuthorizationContext



def is_live_opensearch_available(url: str = "http://localhost:9200") -> bool:
    """Check if a real live OpenSearch cluster is responding at URL."""
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/_cluster/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_AVAILABLE = is_live_opensearch_available(OPENSEARCH_URL)


@pytest.mark.skipif(not OPENSEARCH_AVAILABLE, reason=f"Live OpenSearch cluster not reachable at {OPENSEARCH_URL} (Skipping integration suite)")
class TestOpenSearchIntegrationLive:

    @pytest.fixture(scope="class")
    def live_store(self):
        store = OpenSearchStore(
            base_url=OPENSEARCH_URL,
            index_alias="live_test_integration_chunks",
            configured_dimension=384,
        )
        # Ensure fresh index
        store.delete_index("live_test_integration_chunks_v1")
        store.ensure_index_initialized()
        yield store
        # Cleanup
        store.delete_index("live_test_integration_chunks_v1")

    def test_live_cluster_health(self, live_store):
        health = live_store.health_check()
        assert health.status in {"HEALTHY", "DEGRADED"}
        assert health.backend == "opensearch"

    def test_live_index_and_search(self, live_store):
        payload = ChunkPayload(
            chunk_id="live-chunk-1",
            document_id="live-doc-1",
            tenant_id="live-tenant-1",
            document_version=1,
            content="Live OpenSearch integration test document content.",
            content_hash="livehash1",
            embedding=[0.01] * 384,
            metadata={"access_level": "PUBLIC", "source": "integration.pdf"},
        )
        indexed = live_store.index_chunk(payload)
        assert indexed is True

        # Test vector search
        results = live_store.vector_search(query_vector=[0.01] * 384, top_k=5)
        assert len(results) >= 1
        assert results[0].chunk_id == "live-chunk-1"

        # Test BM25 keyword search
        kw_results = live_store.keyword_search(query_text="integration test", top_k=5)
        assert len(kw_results) >= 1
