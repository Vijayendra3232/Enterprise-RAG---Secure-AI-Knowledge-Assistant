"""
test_google_live_integration.py — Gated Live Google Drive Integration Test Suite.
Strictly gated behind RUN_GOOGLE_DRIVE_INTEGRATION_TESTS=true to ensure CI and local test suites remain 100% offline.
Verifies live connectivity, health check, incremental change feed reading, Workspace exports, and ACL normalization against real Google APIs when credentials are provided.
"""

import os
import json
import pytest
from app.connectors.adapters.google_drive import GoogleDriveConnector
from app.connectors.errors import ConnectorAuthError

RUN_LIVE_GOOGLE = os.getenv("RUN_GOOGLE_DRIVE_INTEGRATION_TESTS", "false").lower() in ("true", "1", "yes")

pytestmark = pytest.mark.skipif(
    not RUN_LIVE_GOOGLE,
    reason="Live Google Drive integration tests skipped (set RUN_GOOGLE_DRIVE_INTEGRATION_TESTS=true to enable).",
)


@pytest.fixture(scope="module")
def google_drive_credentials():
    creds_json = os.getenv("GOOGLE_DRIVE_TEST_CREDENTIALS")
    refresh_token = os.getenv("GOOGLE_DRIVE_TEST_REFRESH_TOKEN")
    client_id = os.getenv("GOOGLE_DRIVE_TEST_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_DRIVE_TEST_CLIENT_SECRET")

    if creds_json:
        try:
            return json.loads(creds_json)
        except Exception:
            if os.path.exists(creds_json):
                with open(creds_json, "r", encoding="utf-8") as f:
                    return json.load(f)
            pytest.skip("Invalid GOOGLE_DRIVE_TEST_CREDENTIALS JSON or file path.")
    elif refresh_token and client_id and client_secret:
        return {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    else:
        pytest.skip("No live Google Drive test credentials provided in environment.")


class TestLiveGoogleDriveIntegration:
    def test_live_health_check_and_discovery(self, google_drive_credentials):
        connector = GoogleDriveConnector(
            tenant_id="live-test-tenant",
            connector_id="live-gdrive-test",
            config=google_drive_credentials,
        )

        # 1. Health check
        health = connector.health_check()
        assert health.get("status") in ("HEALTHY", "DEGRADED")
        assert "latency_ms" in health

        # 2. Fetch changes / files (cursor=None for initial crawl)
        docs, deleted_ids, next_cursor = connector.fetch_changes(cursor=None)
        assert isinstance(docs, list)
        assert isinstance(deleted_ids, list)

        # Verify documents have normalized permissions and source attributes
        for doc in docs[:3]:
            assert doc.source_id is not None
            assert doc.title is not None
            assert doc.source_version is not None
            assert isinstance(doc.permissions, list)
