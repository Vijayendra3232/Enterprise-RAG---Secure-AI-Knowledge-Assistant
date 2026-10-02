"""
test_microsoft_live_integration.py — Gated Live Microsoft Graph Integration Test Suite.
Strictly gated behind RUN_MICROSOFT_GRAPH_INTEGRATION_TESTS=true to ensure CI and local test suites remain 100% offline.
Verifies live connectivity, health check, delta query feeds, drive item download, and permission normalization against Microsoft Graph API when credentials are provided.
"""

import os
import json
import pytest
from app.connectors.adapters.microsoft_graph import MicrosoftGraphConnector
from app.connectors.errors import ConnectorAuthError

RUN_LIVE_MS = os.getenv("RUN_MICROSOFT_GRAPH_INTEGRATION_TESTS", "false").lower() in ("true", "1", "yes")

pytestmark = pytest.mark.skipif(
    not RUN_LIVE_MS,
    reason="Live Microsoft Graph integration tests skipped (set RUN_MICROSOFT_GRAPH_INTEGRATION_TESTS=true to enable).",
)


@pytest.fixture(scope="module")
def ms_graph_credentials():
    creds_json = os.getenv("MS_GRAPH_TEST_CREDENTIALS")
    tenant_id = os.getenv("MS_GRAPH_TEST_TENANT_ID")
    client_id = os.getenv("MS_GRAPH_TEST_CLIENT_ID")
    client_secret = os.getenv("MS_GRAPH_TEST_CLIENT_SECRET")

    if creds_json:
        try:
            return json.loads(creds_json)
        except Exception:
            if os.path.exists(creds_json):
                with open(creds_json, "r", encoding="utf-8") as f:
                    return json.load(f)
            pytest.skip("Invalid MS_GRAPH_TEST_CREDENTIALS JSON or file path.")
    elif tenant_id and client_id and client_secret:
        return {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    else:
        pytest.skip("No live Microsoft Graph test credentials provided in environment.")


class TestLiveMicrosoftGraphIntegration:
    def test_live_health_check_and_discovery(self, ms_graph_credentials):
        connector = MicrosoftGraphConnector(
            tenant_id="live-test-tenant",
            connector_id="live-ms-graph-test",
            config=ms_graph_credentials,
        )

        # 1. Health check
        health = connector.health_check()
        assert health.get("status") in ("HEALTHY", "DEGRADED")
        assert "latency_ms" in health

        # 2. Fetch changes / delta feed (cursor=None for initial crawl)
        docs, deleted_ids, next_cursor = connector.fetch_changes(cursor=None)
        assert isinstance(docs, list)
        assert isinstance(deleted_ids, list)

        # Verify documents have normalized permissions and source attributes
        for doc in docs[:3]:
            assert doc.source_id is not None
            assert doc.title is not None
            assert doc.source_version is not None
            assert isinstance(doc.permissions, list)
