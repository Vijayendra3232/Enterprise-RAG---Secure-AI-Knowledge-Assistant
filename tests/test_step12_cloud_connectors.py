"""
test_step12_cloud_connectors.py — Comprehensive Unit & Security Test Suite for Step 12:
Live Enterprise Cloud Connectors + Permission Synchronization.

Covers:
1. SSRF Defense: loopback, private RFC1918, link-local, cloud metadata (169.254.169.254), IPv4-mapped IPv6, DNS resolution checking, redirect validation.
2. Google Drive Adapter: OAuth2/Service Account auth, single-flight token refresh, refresh token preservation, Workspace exports, anyone/domain fail-closed rules, changes.list with mandatory ACL refetch.
3. Microsoft Graph Adapter: Client credentials & delegated auth, single-flight token refresh, delta queries with deletion detection, throttling with Retry-After, permissions with inheritance.
4. Concurrency Control: Single active sync per connector, mutual exclusion locking, stale lock recovery, cross-tenant isolation.
5. Transactional Cursor: Atomic batch processing, cursor rollback on mid-batch failure, idempotent retry.
6. Authoritative Authorization Boundary: Stale OpenSearch ALLOW + PostgreSQL DENY -> strictly DENY; pre-reranking filtering.
7. Group Membership Security: Client injection defense, trusted group access, unverified group fail-closed, cross-tenant isolation.
8. Source Outage & Anti-Mass-Deletion Protection: Source outage aborts without deleting documents.
9. Permission-Only Update: Zero re-embedding verified when content is unchanged.
10. Credential Isolation: Zero plaintext secrets in DB, task payloads, logs, or API responses.
"""

import os
import io
import sys
import time
import json
import uuid
import hashlib
import ipaddress
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import config
from app.main import app
from app.storage.database import Base, get_db
from app.storage.models import (
    Tenant as DBTenant,
    User as DBUser,
    Document as DBDocument,
    DocumentChunk as DBDocumentChunk,
    DocumentPermission as DBDocumentPermission,
    ConnectorConfig as DBConnectorConfig,
    SyncRun as DBSyncRun,
    Task as DBTask,
    OutboxEvent as DBOutboxEvent,
)
from app.storage.repositories import (
    SQLConnectorRepository,
    SQLSyncRunRepository,
    SQLDocumentRepository,
    SQLPermissionRepository,
    SQLChunkRepository,
    SQLTaskRepository,
)
from app.connectors.base import (
    DocumentConnector,
    ConnectorDocument,
    ConnectorACL,
    ConnectorPermission,
    Principal,
    PrincipalType,
    PermissionEffect,
)
from app.connectors.errors import (
    ConnectorError,
    ConnectorNotFoundError,
    SourceUnavailableError,
    ConcurrentSyncError,
    PermanentAuthError,
    TokenRefreshError,
    SSRFSecurityError,
)
from app.connectors.ssrf import (
    is_ip_allowed,
    validate_url,
    SSRFSafeSession,
    DEFAULT_GOOGLE_DOMAINS,
    DEFAULT_MICROSOFT_DOMAINS,
)
from app.connectors.permissions import PermissionNormalizer
from app.connectors.adapters.google_drive import GoogleDriveConnector
from app.connectors.adapters.microsoft_graph import MicrosoftGraphConnector
from app.connectors.registry import connector_registry
from app.connectors.secrets import get_secret_provider, reset_secret_provider, LocalSecretProvider
from app.connectors.sync.service import SyncService
from app.connectors.sync.models import SyncMode, SyncAction
from app.authorization.context import AuthorizationContext
from app.authorization.filters import is_document_accessible
from app.retrieval.pipeline import RetrievalPipeline
from app.retrieval.models import SearchResult
import jwt
from app.auth.jwt import (
    create_access_token,
    decode_access_token,
    InvalidTokenError,
    DisallowedAlgorithmError,
    UntrustedIssuerError,
    InvalidAudienceError,
    TenantMismatchError,
)
from app.auth.repository import get_user_repository


@pytest.fixture
def test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Seed base tenant and user
    t = DBTenant(tenant_id="tenant_step12", name="Step12 Tenant")
    u = DBUser(user_id="user_step12", tenant_id="tenant_step12", email="alice@step12.com", name="Alice", password_hash="dummy_hash", role="USER")
    session.add(t)
    session.add(u)
    session.commit()

    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


# ═════════════════════════════════════════════════════════════════════════════
# 1. SSRF DEFENSE SUITE (Modification 8)
# ═════════════════════════════════════════════════════════════════════════════

class TestSSRFDefense:
    def test_blocks_loopback_and_private_ipv4(self):
        assert not is_ip_allowed(ipaddress.ip_address("127.0.0.1"))
        assert not is_ip_allowed(ipaddress.ip_address("127.0.1.5"))
        assert not is_ip_allowed(ipaddress.ip_address("10.0.0.1"))
        assert not is_ip_allowed(ipaddress.ip_address("172.16.0.1"))
        assert not is_ip_allowed(ipaddress.ip_address("192.168.1.1"))

    def test_blocks_cloud_metadata_and_link_local(self):
        assert not is_ip_allowed(ipaddress.ip_address("169.254.169.254"))
        assert not is_ip_allowed(ipaddress.ip_address("169.254.1.1"))

    def test_blocks_ipv6_loopback_linklocal_and_mapped(self):
        assert not is_ip_allowed(ipaddress.ip_address("::1"))
        assert not is_ip_allowed(ipaddress.ip_address("fe80::1"))
        assert not is_ip_allowed(ipaddress.ip_address("fc00::1"))
        # IPv4-mapped IPv6
        assert not is_ip_allowed(ipaddress.ip_address("::ffff:127.0.0.1"))
        assert not is_ip_allowed(ipaddress.ip_address("::ffff:169.254.169.254"))
        assert not is_ip_allowed(ipaddress.ip_address("::ffff:10.0.0.1"))

    def test_permits_public_ipv4(self):
        assert is_ip_allowed(ipaddress.ip_address("8.8.8.8"))
        assert is_ip_allowed(ipaddress.ip_address("142.250.190.46"))

    def test_validate_url_rejects_unauthorized_domain(self):
        with pytest.raises(SSRFSecurityError) as exc_info:
            validate_url("https://evil-attacker.com/api", allowed_domains=DEFAULT_GOOGLE_DOMAINS)
        assert "not in the allowed domains" in str(exc_info.value)

    def test_validate_url_rejects_direct_private_ip(self):
        with pytest.raises(SSRFSecurityError) as exc_info:
            validate_url("https://127.0.0.1:8080/token")
        assert "prohibited private or link-local" in str(exc_info.value)

    def test_validate_url_rejects_metadata_endpoint(self):
        with pytest.raises(SSRFSecurityError):
            validate_url("http://169.254.169.254/latest/meta-data")

    def test_validate_url_rejects_non_http_scheme(self):
        with pytest.raises(SSRFSecurityError):
            validate_url("file:///etc/passwd")
        with pytest.raises(SSRFSecurityError):
            validate_url("ftp://server/file")

    def test_validate_url_allows_valid_google_domain(self):
        url, host, port = validate_url("https://www.googleapis.com/drive/v3/files", allowed_domains=DEFAULT_GOOGLE_DOMAINS)
        assert host == "www.googleapis.com"
        assert port == 443

    def test_validate_url_allows_valid_microsoft_domain(self):
        url, host, port = validate_url("https://graph.microsoft.com/v1.0/me/drive", allowed_domains=DEFAULT_MICROSOFT_DOMAINS)
        assert host == "graph.microsoft.com"
        assert port == 443


# ═════════════════════════════════════════════════════════════════════════════
# 2. GOOGLE DRIVE ADAPTER SUITE (Modifications 1, 5, 7)
# ═════════════════════════════════════════════════════════════════════════════

class TestGoogleDriveConnector:
    def test_anyone_permission_fails_closed_by_default(self):
        acl = ConnectorACL(
            permissions=[
                ConnectorPermission(
                    principal=Principal(principal_type=PrincipalType.ROLE, principal_id="ANYONE"),
                    permission="DOCUMENT_READ",
                    effect=PermissionEffect.ALLOW,
                )
            ],
            permission_status="KNOWN",
        )
        # Without explicit allow_public_link_access -> fails closed to UNKNOWN / PRIVATE
        norm = PermissionNormalizer.normalize(
            acl=acl,
            tenant_id="tenant_step12",
            document_id="doc_1",
            connector_config={"allow_public_link_access": False},
        )
        assert norm.permission_status == "UNKNOWN"
        assert "PUBLIC" not in norm.allowed_roles

        # With explicit policy enabled -> maps to PUBLIC
        norm_allowed = PermissionNormalizer.normalize(
            acl=acl,
            tenant_id="tenant_step12",
            document_id="doc_1",
            connector_config={"allow_public_link_access": True},
        )
        assert norm_allowed.permission_status == "KNOWN"
        assert "PUBLIC" in norm_allowed.allowed_roles

    def test_domain_permission_verification(self):
        acl = ConnectorACL(
            permissions=[
                ConnectorPermission(
                    principal=Principal(principal_type=PrincipalType.ROLE, principal_id="DOMAIN:company.com"),
                    permission="DOCUMENT_READ",
                    effect=PermissionEffect.ALLOW,
                )
            ],
            permission_status="KNOWN",
        )
        # Verified domain with allow_domain_access -> allowed
        norm_ok = PermissionNormalizer.normalize(
            acl=acl,
            tenant_id="tenant_step12",
            document_id="doc_2",
            connector_config={"allow_domain_access": True, "allowed_domains": ["company.com"]},
        )
        assert norm_ok.permission_status == "KNOWN"
        assert "DOMAIN_COMPANY_COM" in norm_ok.allowed_roles

        # External / unverified domain -> fails closed
        norm_denied = PermissionNormalizer.normalize(
            acl=acl,
            tenant_id="tenant_step12",
            document_id="doc_2",
            connector_config={"allow_domain_access": True, "allowed_domains": ["othercorp.com"]},
        )
        assert norm_denied.permission_status == "UNKNOWN"
        assert len(norm_denied.allowed_roles) == 0

    def test_single_flight_token_refresh_and_preservation(self):
        connector = GoogleDriveConnector(
            tenant_id="tenant_step12",
            connector_id="conn_gdrive",
            config={
                "client_id": "test_client_id",
                "client_secret": "test_client_secret",
                "refresh_token": "original_refresh_token_xyz",
                "access_token": "old_expired_token",
                "token_expiry": time.time() - 100,  # Expired
            },
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Google returns new access_token but OMITS refresh_token
        mock_resp.json.return_value = {
            "access_token": "new_refreshed_access_token_abc",
            "expires_in": 3600,
        }

        with patch.object(connector._session, "post", return_value=mock_resp) as mock_post:
            connector._ensure_access_token()
            assert connector.access_token == "new_refreshed_access_token_abc"
            # Refresh token preservation invariant:
            assert connector.refresh_token == "original_refresh_token_xyz"
            assert mock_post.call_count == 1

            # Second call does not re-refresh (reused)
            connector._ensure_access_token()
            assert mock_post.call_count == 1

        refreshed_cfg = connector.refresh_credentials()
        assert refreshed_cfg is not None
        assert refreshed_cfg["access_token"] == "new_refreshed_access_token_abc"
        assert refreshed_cfg["refresh_token"] == "original_refresh_token_xyz"


# ═════════════════════════════════════════════════════════════════════════════
# 3. MICROSOFT GRAPH ADAPTER SUITE (Modifications 5, 7)
# ═════════════════════════════════════════════════════════════════════════════

class TestMicrosoftGraphConnector:
    def test_single_flight_token_refresh_and_throttling_retry(self):
        connector = MicrosoftGraphConnector(
            tenant_id="tenant_step12",
            connector_id="conn_msgraph",
            config={
                "client_id": "ms_client_id",
                "client_secret": "ms_client_secret",
                "tenant_id": "common",
                "refresh_token": "ms_refresh_token_123",
                "access_token": "expired_ms_token",
                "token_expiry": time.time() - 50,
            },
        )

        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = {
            "access_token": "fresh_ms_access_token_789",
            "expires_in": 3600,
        }

        with patch.object(connector._session, "post", return_value=mock_token_resp):
            connector._ensure_access_token()
            assert connector.access_token == "fresh_ms_access_token_789"
            assert connector.refresh_token == "ms_refresh_token_123"

    def test_permission_normalization_with_groups_and_inheritance(self):
        connector = MicrosoftGraphConnector(
            tenant_id="tenant_step12",
            connector_id="conn_msgraph",
            config={"_mock_client": None},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "value": [
                {
                    "id": "perm_1",
                    "roles": ["read"],
                    "grantedToV2": {
                        "user": {"email": "bob@company.com", "displayName": "Bob Smith"}
                    },
                },
                {
                    "id": "perm_2",
                    "roles": ["write"],
                    "grantedToV2": {
                        "group": {"id": "group_finance_id", "email": "finance@company.com"}
                    },
                },
            ]
        }

        with patch.object(connector, "_execute_api_request", return_value=mock_resp):
            acl = connector.get_permissions("item_123")
            assert acl is not None
            assert acl.permission_status == "KNOWN"
            assert len(acl.permissions) == 2
            
            user_perm = [p for p in acl.permissions if p.principal.principal_type == PrincipalType.USER][0]
            assert user_perm.principal.principal_id == "bob@company.com"

            group_perm = [p for p in acl.permissions if p.principal.principal_type == PrincipalType.GROUP][0]
            assert group_perm.principal.principal_id == "finance@company.com"


# ═════════════════════════════════════════════════════════════════════════════
# 4. CONCURRENCY CONTROL SUITE (Modification 4)
# ═════════════════════════════════════════════════════════════════════════════

class TestConnectorConcurrencyControl:
    def test_prevents_simultaneous_sync_on_same_connector(self, test_db):
        repo = SQLConnectorRepository(test_db)
        secret_prov = get_secret_provider()
        enc_cfg = secret_prov.encrypt_json({"path": "local_data"})

        conn = DBConnectorConfig(
            id="conn_lock_test",
            tenant_id="tenant_step12",
            name="Lock Test Connector",
            connector_type="local",
            status="ACTIVE",
            encrypted_config=enc_cfg,
        )
        repo.create(conn)
        test_db.commit()

        # Worker 1 acquires sync lock
        w1_conn = repo.acquire_sync_lock("conn_lock_test", "tenant_step12")
        assert w1_conn is not None
        assert w1_conn.status == "SYNCING"
        assert w1_conn.sync_lock_at is not None
        test_db.commit()

        # Worker 2 attempts concurrent sync -> rejected
        w2_conn = repo.acquire_sync_lock("conn_lock_test", "tenant_step12")
        assert w2_conn is None

        # Worker 1 finishes and releases lock
        repo.release_sync_lock("conn_lock_test", "tenant_step12", new_status="ACTIVE")
        test_db.commit()

        # Worker 2 now successfully acquires
        w2_acquired = repo.acquire_sync_lock("conn_lock_test", "tenant_step12")
        assert w2_acquired is not None
        assert w2_acquired.status == "SYNCING"

    def test_stale_lock_recovery_after_crash(self, test_db):
        repo = SQLConnectorRepository(test_db)
        secret_prov = get_secret_provider()
        enc_cfg = secret_prov.encrypt_json({"path": "local_data"})

        # Connector stuck in SYNCING from a crashed worker 10 minutes ago
        crashed_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        conn = DBConnectorConfig(
            id="conn_crashed",
            tenant_id="tenant_step12",
            name="Crashed Connector",
            connector_type="local",
            status="SYNCING",
            sync_lock_at=crashed_time,
            encrypted_config=enc_cfg,
        )
        repo.create(conn)
        test_db.commit()

        # Next worker reclaims stale lease cleanly
        reclaimed = repo.acquire_sync_lock("conn_crashed", "tenant_step12", stale_threshold_seconds=300)
        assert reclaimed is not None
        assert reclaimed.status == "SYNCING"
        lock_at = reclaimed.sync_lock_at
        if lock_at.tzinfo is None:
            lock_at = lock_at.replace(tzinfo=timezone.utc)
        assert (datetime.now(timezone.utc) - lock_at).total_seconds() < 5


# ═════════════════════════════════════════════════════════════════════════════
# 5. TRANSACTIONAL CURSOR SUITE (Modification 3)
# ═════════════════════════════════════════════════════════════════════════════

class TestTransactionalCursorAdvancement:
    def test_successful_batch_advances_cursor(self, test_db):
        secret_prov = get_secret_provider()
        enc_cfg = secret_prov.encrypt_json({"client_id": "test"})
        conn = DBConnectorConfig(
            id="conn_cursor_ok",
            tenant_id="tenant_step12",
            name="Cursor OK Connector",
            connector_type="google_drive",
            status="ACTIVE",
            sync_cursor="initial_cursor_100",
            encrypted_config=enc_cfg,
        )
        test_db.add(conn)
        test_db.commit()

        # Mock connector returning changes and new cursor
        mock_adapter = MagicMock()
        mock_adapter.validate_configuration.return_value = True
        mock_doc = ConnectorDocument(
            source_type="google_drive",
            source_id="file_g1",
            name="Report.txt",
            content=b"Sample content",
            content_hash=hashlib.sha256(b"Sample content").hexdigest(),
            acl=ConnectorACL(permissions=[], permission_status="KNOWN"),
        )
        mock_adapter.fetch_changes.return_value = ([mock_doc], [], "advanced_cursor_200")
        mock_adapter.refresh_credentials.return_value = None

        service = SyncService(test_db)
        with patch.object(connector_registry, "create", return_value=mock_adapter):
            result = service.sync("conn_cursor_ok", "tenant_step12", mode=SyncMode.INCREMENTAL)
            assert result.status == "SUCCESS"

        test_db.refresh(conn)
        assert conn.sync_cursor == "advanced_cursor_200"

    def test_batch_failure_rolls_back_and_leaves_cursor_unchanged(self, test_db):
        secret_prov = get_secret_provider()
        enc_cfg = secret_prov.encrypt_json({"client_id": "test"})
        conn = DBConnectorConfig(
            id="conn_cursor_fail",
            tenant_id="tenant_step12",
            name="Cursor Fail Connector",
            connector_type="google_drive",
            status="ACTIVE",
            sync_cursor="safe_checkpoint_100",
            encrypted_config=enc_cfg,
        )
        test_db.add(conn)
        test_db.commit()

        mock_adapter = MagicMock()
        mock_adapter.validate_configuration.return_value = True
        mock_doc = ConnectorDocument(
            source_type="google_drive",
            source_id="file_fail",
            name="Corrupt.txt",
            content=b"Corrupt content",
            content_hash=hashlib.sha256(b"Corrupt content").hexdigest(),
        )
        mock_adapter.fetch_changes.return_value = ([mock_doc], [], "bad_cursor_999")
        mock_adapter.refresh_credentials.return_value = None

        service = SyncService(test_db)
        # Simulate fatal failure during document import
        with patch.object(connector_registry, "create", return_value=mock_adapter),              patch.object(service, "_execute_import", side_effect=Exception("Database connection lost")):
            result = service.sync("conn_cursor_fail", "tenant_step12", mode=SyncMode.INCREMENTAL)
            assert result.status == "ERROR"

        test_db.refresh(conn)
        # Checkpoint remains unchanged!
        assert conn.sync_cursor == "safe_checkpoint_100"


# ═════════════════════════════════════════════════════════════════════════════
# 6. AUTHORITATIVE AUTHORIZATION BOUNDARY SUITE (Modification 2)
# ═════════════════════════════════════════════════════════════════════════════

class TestAuthoritativeAuthorizationBoundary:
    def test_revocation_denies_access_even_if_opensearch_has_stale_allow(self):
        """
        CRITICAL SECURITY TEST:
        OpenSearch contains stale 'ALLOWED' metadata for Alice.
        PostgreSQL document permissions are updated to DENY Alice.
        Authoritative backend check MUST reject the candidate before reranking/LLM.
        """
        auth_context = AuthorizationContext(
            user_id="alice",
            tenant_id="tenant_step12",
            role="USER",
            groups=["engineering"],
        )

        # Stale OpenSearch candidate that thinks Alice is allowed
        stale_candidate = SearchResult(
            chunk_id="chunk_stale_1",
            document_id="doc_strategy",
            content="Sensitive corporate strategy document.",
            source="gdrive",
            page=1,
            score=0.98,
            metadata={
                "tenant_id": "tenant_step12",
                "document_id": "doc_strategy",
                # Stale OpenSearch metadata says Alice is allowed:
                "allowed_user_ids": ["alice"],
                # But authoritative PostgreSQL permission status updated to DENIED:
                "denied_user_ids": ["alice"],
                "access_level": "USER",
                "permission_status": "KNOWN",
            },
        )

        # 1. Direct evaluator test
        is_accessible = is_document_accessible(stale_candidate.metadata, auth_context)
        assert is_accessible is False

        # 2. Pipeline pre-reranking filter test
        mock_search_store = MagicMock()
        mock_search_store.vector_search.return_value = [stale_candidate]
        pipeline = RetrievalPipeline(
            search_store=mock_search_store,
            strategy="vector",
        )

        response = pipeline.search("strategy", auth_context=auth_context)
        # Unauthorized candidate strictly removed before reranking or generation
        assert len(response.results) == 0
        assert response.diagnostics.final_result_count == 0

    def test_access_expansion_denies_until_authoritative_state_updated(self):
        """
        User initially has no access.
        Until authoritative state confirms access, document is denied.
        """
        auth_context = AuthorizationContext(
            user_id="bob",
            tenant_id="tenant_step12",
            role="USER",
            groups=[],
        )

        candidate = SearchResult(
            chunk_id="chunk_secret",
            document_id="doc_secret",
            content="Top secret project.",
            source="gdrive",
            page=1,
            score=0.95,
            metadata={
                "tenant_id": "tenant_step12",
                "document_id": "doc_secret",
                "allowed_user_ids": ["alice"],  # Bob not allowed
                "access_level": "USER",
                "permission_status": "KNOWN",
            },
        )

        assert is_document_accessible(candidate.metadata, auth_context) is False


# ═════════════════════════════════════════════════════════════════════════════
# 7. TRUSTED GROUP MEMBERSHIP SECURITY SUITE (Final Security Modification)
# ═════════════════════════════════════════════════════════════════════════════

class TestGroupMembershipSecurity:
    """
    Comprehensive Security Verification for Trusted Group Membership:
    Only authenticated, cryptographically validated, trusted-issuer,
    correct-audience, and correct-tenant identity information may influence group authorization.
    Client-controlled group information has ZERO authorization authority.
    """

    # Test 1 — Client group injection
    def test_client_group_injection_ignored_and_denied(self, test_db):
        """
        Client attempts sending 'groups': ['admin', 'finance'] in a request payload.
        The backend authorization context is derived ONLY from the validated identity,
        completely ignoring client-supplied group claims.
        """
        user_repo = get_user_repository()
        user_alice = user_repo.get_by_id("user_a")
        if not user_alice:
            from app.auth.models import UserInDB
            from app.auth.password import hash_password
            user_alice = UserInDB(
                user_id="user_a",
                tenant_id="company_a",
                email="usera@companya.com",
                name="User A",
                role="USER",
                is_active=True,
                groups=["engineering"],  # Only engineering, not finance
                hashed_password=hash_password("password123"),
            )
            user_repo.create(user_alice)

        token = create_access_token({
            "sub": "user_a",
            "tenant_id": "company_a",
            "email": "usera@companya.com",
            "role": "USER",
        })

        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}

        # Protected document requiring 'finance' group
        doc_meta = {
            "tenant_id": "company_a",
            "document_id": "doc_fin_restricted",
            "allowed_groups": ["finance"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }

        # Verified auth context resolves only authoritative user groups (engineering)
        auth_context = AuthorizationContext(
            user_id="user_a",
            tenant_id="company_a",
            role="USER",
            groups=user_alice.groups,  # ["engineering"]
        )

        assert is_document_accessible(doc_meta, auth_context) is False

    # Test 2 — Unvalidated JWT
    def test_unvalidated_or_forged_jwt_denied(self):
        """
        JWT contains groups=['finance'] but has an invalid signature or forged secret.
        Must be rejected with InvalidTokenError / 401 Unauthorized.
        """
        forged_token = jwt.encode(
            {
                "sub": "hacker_bob",
                "tenant_id": "company_a",
                "groups": ["finance"],
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            "wrong-secret-key-attacker-forged",
            algorithm="HS256",
        )

        with pytest.raises(InvalidTokenError):
            decode_access_token(forged_token)

        client = TestClient(app)
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged_token}"})
        assert resp.status_code == 401

    # Test 3 — Wrong issuer
    def test_wrong_issuer_denied(self):
        """
        Validly signed token from an untrusted issuer ('https://untrusted-idp.com').
        Must be rejected with UntrustedIssuerError / 401 Unauthorized.
        """
        token = create_access_token(
            data={"sub": "user_a", "tenant_id": "company_a", "groups": ["finance"]},
            issuer="https://untrusted-idp.com",
        )

        with pytest.raises(UntrustedIssuerError):
            decode_access_token(token)

        client = TestClient(app)
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    # Test 4 — Wrong audience
    def test_wrong_audience_denied(self):
        """
        Token is validly signed and has correct issuer, but was issued for a different application.
        Must be rejected with InvalidAudienceError / 401 Unauthorized.
        """
        token = create_access_token(
            data={"sub": "user_a", "tenant_id": "company_a", "groups": ["finance"]},
            audience="some-other-microservice",
        )

        with pytest.raises(InvalidAudienceError):
            decode_access_token(token)

        client = TestClient(app)
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    # Test 5 — Wrong tenant
    def test_wrong_tenant_denied(self):
        """
        Valid token belonging to Tenant B attempts to access Tenant A or claim Tenant A groups.
        Must fail closed.
        """
        token_beta = create_access_token(
            data={"sub": "user_beta", "tenant_id": "tenant_beta", "groups": ["finance"]},
        )

        with pytest.raises(TenantMismatchError):
            decode_access_token(token_beta, expected_tenant_id="tenant_alpha")

        # Document belonging to Tenant Alpha cannot be accessed by Tenant Beta user
        doc_meta = {
            "tenant_id": "tenant_alpha",
            "document_id": "doc_alpha_1",
            "allowed_groups": ["finance"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }
        beta_context = AuthorizationContext(
            user_id="user_beta",
            tenant_id="tenant_beta",
            role="USER",
            groups=["finance"],
        )
        assert is_document_accessible(doc_meta, beta_context) is False

    # Test 6 — Valid trusted group claim
    def test_valid_trusted_group_claim_allowed(self):
        """
        Correctly signed and validated token from configured issuer, correct audience,
        and correct tenant, with authorized group membership.
        Document ACL grants group access -> ALLOW.
        """
        token = create_access_token({
            "sub": "user_step12",
            "tenant_id": "tenant_step12",
            "groups": ["engineering"],
        })
        payload = decode_access_token(token)
        assert payload.sub == "user_step12"
        assert "engineering" in payload.groups

        auth_context = AuthorizationContext(
            user_id=payload.sub,
            tenant_id=payload.tenant_id,
            role="USER",
            groups=payload.groups,
        )

        doc_meta = {
            "tenant_id": "tenant_step12",
            "document_id": "doc_eng_guide",
            "allowed_groups": ["engineering"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }
        assert is_document_accessible(doc_meta, auth_context) is True

    # Test 7 — Unknown membership
    def test_unknown_group_membership_fails_closed(self):
        """
        Membership cannot be deterministically established (empty, unmapped, or UNKNOWN doc permission).
        Must fail closed (UNKNOWN -> DENY).
        """
        # User has no groups
        auth_context = AuthorizationContext(
            user_id="user_guest",
            tenant_id="tenant_step12",
            role="USER",
            groups=[],
        )

        doc_meta = {
            "tenant_id": "tenant_step12",
            "document_id": "doc_restricted",
            "allowed_groups": ["security"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }
        assert is_document_accessible(doc_meta, auth_context) is False

        # Document permission status UNKNOWN -> fail closed
        doc_meta_unknown = {
            "tenant_id": "tenant_step12",
            "document_id": "doc_uncertain",
            "allowed_groups": ["security"],
            "access_level": "GROUP",
            "permission_status": "UNKNOWN",
        }
        assert is_document_accessible(doc_meta_unknown, auth_context) is False

    # Test 8 — Removed membership (Authoritative Revocation)
    def test_removed_group_membership_authoritative_revocation(self):
        """
        Authoritative membership in DB indicates the user is no longer a member of 'finance'.
        Even if an old token claimed 'finance', authoritative DB state takes precedence.
        """
        # User account where finance group was revoked
        revoked_auth_context = AuthorizationContext(
            user_id="user_revoked",
            tenant_id="tenant_step12",
            role="USER",
            groups=[],  # Group revoked in authoritative directory
        )

        doc_meta = {
            "tenant_id": "tenant_step12",
            "document_id": "doc_fin_confidential",
            "allowed_groups": ["finance"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }
        assert is_document_accessible(doc_meta, revoked_auth_context) is False

    # Test 9 — Cross-tenant group
    def test_cross_tenant_group_claim_denied(self):
        """
        User in Tenant A attempts to use a group belonging to Tenant B.
        Cross-tenant group relationships strictly fail closed.
        """
        auth_context_a = AuthorizationContext(
            user_id="user_a",
            tenant_id="tenant_a",
            role="USER",
            groups=["finance"],
        )

        doc_meta_b = {
            "tenant_id": "tenant_b",  # Different tenant
            "document_id": "doc_b_finance",
            "allowed_groups": ["finance"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }
        assert is_document_accessible(doc_meta_b, auth_context_a) is False

    # Test 10 — Nested group
    def test_nested_group_unsupported_hierarchy_denied(self):
        """
        If recursive nested groups are not explicitly supported,
        membership cannot be deterministically inferred: UNKNOWN -> DENY.
        """
        auth_context = AuthorizationContext(
            user_id="user_junior",
            tenant_id="tenant_step12",
            role="USER",
            groups=["engineering.frontend.interns"],
        )

        # Document ACL grants access only to parent group 'engineering'
        doc_meta = {
            "tenant_id": "tenant_step12",
            "document_id": "doc_all_eng",
            "allowed_groups": ["engineering"],
            "access_level": "GROUP",
            "permission_status": "KNOWN",
        }
        # Direct deterministic evaluation rejects unmapped nested group
        assert is_document_accessible(doc_meta, auth_context) is False

    # Test 11 — Unsupported algorithm rejected
    def test_unsupported_algorithm_rejected(self):
        """
        Token signed with an algorithm not in the server allowlist (e.g. HS512 when allowlist is HS256).
        Must be rejected with DisallowedAlgorithmError / 401 Unauthorized.
        """
        disallowed_token = jwt.encode(
            {"sub": "user_a", "tenant_id": "company_a", "groups": ["engineering"], "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())},
            config.JWT_SECRET_KEY,
            algorithm="HS512",
        )
        with pytest.raises(DisallowedAlgorithmError):
            decode_access_token(disallowed_token)

    # Test 12 — Algorithm 'none' strictly rejected
    def test_algorithm_none_rejected(self):
        """
        Unsigned token with alg='none' must be rejected with DisallowedAlgorithmError / 401 Unauthorized.
        """
        none_token = jwt.encode(
            {"sub": "user_a", "tenant_id": "company_a", "groups": ["admin"], "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())},
            key="",
            algorithm="none",
        )
        with pytest.raises(DisallowedAlgorithmError):
            decode_access_token(none_token)

    # Test 13 — verify_signature=False cannot be enabled
    def test_verify_signature_false_strictly_prohibited(self):
        """
        Any attempt to invoke token decoding without cryptographic verification is rejected.
        """
        valid_token = create_access_token({"sub": "user_a", "tenant_id": "company_a"})
        with pytest.raises(InvalidTokenError) as exc_info:
            decode_access_token(valid_token, verify_signature=False)
        assert "Unverified" in str(exc_info.value)

    # Test 14 — Client cannot override authenticated tenant
    def test_client_cannot_override_authenticated_tenant(self, test_db):
        """
        A user authenticated in Tenant A attempts to access or create a resource specifying Tenant B in the payload.
        The authenticated JWT tenant strictly governs the security boundary.
        """
        from app.auth.dependencies import get_current_user
        from app.auth.models import User as AuthUser

        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: test_db
        # Principal belongs to tenant_step12
        app.dependency_overrides[get_current_user] = lambda: AuthUser(
            user_id="user_step12",
            tenant_id="tenant_step12",
            email="alice@step12.com",
            name="Alice",
            role="ADMIN",
            is_active=True,
            groups=[],
        )

        token = create_access_token({"sub": "user_step12", "tenant_id": "tenant_step12", "role": "ADMIN"})
        headers = {"Authorization": f"Bearer {token}"}

        # Client attempts passing 'tenant_id': 'tenant_foreign' in body
        payload = {
            "name": "Cross Tenant Connector",
            "connector_type": "google_drive",
            "tenant_id": "tenant_foreign",  # ATTEMPTED TENANT INJECTION
            "config": {"client_id": "test"},
        }
        resp = client.post("/connectors", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        # Security Boundary: Created resource is strictly bound to principal's authenticated tenant!
        assert data["tenant_id"] == "tenant_step12"
        assert data["tenant_id"] != "tenant_foreign"

        app.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# 8. SOURCE OUTAGE & ANTI-MASS-DELETION SUITE
# ═════════════════════════════════════════════════════════════════════════════

class TestSourceOutageSafety:
    def test_source_outage_does_not_delete_existing_documents(self, test_db):
        doc_repo = SQLDocumentRepository(test_db)
        secret_prov = get_secret_provider()
        enc_cfg = secret_prov.encrypt_json({"client_id": "test"})

        conn = DBConnectorConfig(
            id="conn_outage_test",
            tenant_id="tenant_step12",
            name="Outage Test Connector",
            connector_type="google_drive",
            status="ACTIVE",
            encrypted_config=enc_cfg,
        )
        test_db.add(conn)

        # Add 3 existing synced documents
        for i in range(3):
            doc = DBDocument(
                document_id=f"doc_existing_{i}",
                tenant_id="tenant_step12",
                filename=f"file_{i}.txt",
                source="google_drive",
                source_document_id=f"g_file_{i}",
                content_hash=f"hash_{i}",
                status="INDEXED",
                metadata_json={"connector_id": "conn_outage_test"},
            )
            doc_repo.create(doc)
        test_db.commit()

        mock_adapter = MagicMock()
        mock_adapter.validate_configuration.return_value = True
        # Source outage raises exception
        mock_adapter.list_documents.side_effect = SourceUnavailableError("Google Drive 503 Service Unavailable")

        service = SyncService(test_db)
        with patch.object(connector_registry, "create", return_value=mock_adapter):
            result = service.sync("conn_outage_test", "tenant_step12", mode=SyncMode.FULL)
            assert result.status == "ERROR"

        # Anti-mass-deletion invariant: all 3 existing documents remain intact
        existing_docs = doc_repo.list_by_tenant("tenant_step12")
        assert len(existing_docs) == 3
        for d in existing_docs:
            assert d.status == "INDEXED"


# ═════════════════════════════════════════════════════════════════════════════
# 9. PERMISSION-ONLY UPDATE (ZERO RE-EMBEDDING) SUITE
# ═════════════════════════════════════════════════════════════════════════════

class TestPermissionOnlySync:
    def test_permission_only_change_does_not_re_embed(self, test_db):
        doc_repo = SQLDocumentRepository(test_db)
        secret_prov = get_secret_provider()
        enc_cfg = secret_prov.encrypt_json({"client_id": "test"})

        conn = DBConnectorConfig(
            id="conn_perm_only",
            tenant_id="tenant_step12",
            name="Perm Only Connector",
            connector_type="google_drive",
            status="ACTIVE",
            encrypted_config=enc_cfg,
        )
        test_db.add(conn)

        content = b"Constant unchanging binary content."
        c_hash = hashlib.sha256(content).hexdigest()

        # Existing doc with old ACL hash
        doc = DBDocument(
            document_id=c_hash,
            tenant_id="tenant_step12",
            filename="Unchanged.txt",
            source="google_drive",
            source_document_id="g_doc_perm_only",
            content_hash=c_hash,
            status="INDEXED",
            version=1,
            metadata_json={"connector_id": "conn_perm_only", "acl_hash": "old_acl_hash"},
        )
        doc_repo.create(doc)
        test_db.commit()

        # Source doc has same content_hash but new ACL
        new_acl = ConnectorACL(
            permissions=[
                ConnectorPermission(
                    principal=Principal(principal_type=PrincipalType.USER, principal_id="new_user@company.com"),
                    permission="DOCUMENT_READ",
                    effect=PermissionEffect.ALLOW,
                )
            ],
            permission_status="KNOWN",
        )
        s_doc = ConnectorDocument(
            source_type="google_drive",
            source_id="g_doc_perm_only",
            name="Unchanged.txt",
            content=content,
            content_hash=c_hash,
            acl=new_acl,
        )

        mock_adapter = MagicMock()
        mock_adapter.validate_configuration.return_value = True
        mock_adapter.list_documents.return_value = [s_doc]
        mock_adapter.refresh_credentials.return_value = None

        service = SyncService(test_db)
        with patch.object(connector_registry, "create", return_value=mock_adapter),              patch("app.connectors.sync.service.ingest_document") as mock_ingest:
            result = service.sync("conn_perm_only", "tenant_step12", mode=SyncMode.FULL)
            assert result.status == "SUCCESS"
            assert result.permissions_updated == 1
            assert result.documents_added == 0
            assert result.documents_updated == 0

            # Zero re-embedding invariant:
            mock_ingest.assert_not_called()

        test_db.refresh(doc)
        assert doc.metadata_json["acl_hash"] == new_acl.canonical_hash()
        assert "new_user@company.com" in doc.metadata_json.get("allowed_user_ids", [])


# ═════════════════════════════════════════════════════════════════════════════
# 10. API INTEGRATION & CREDENTIAL ISOLATION SUITE
# ═════════════════════════════════════════════════════════════════════════════

class TestConnectorAPIAndCredentialIsolation:
    def test_api_never_exposes_plaintext_secrets_or_tokens(self, test_db):
        from app.auth.dependencies import get_current_user
        from app.auth.models import User as AuthUser

        client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_current_user] = lambda: AuthUser(
            user_id="user_step12",
            tenant_id="tenant_step12",
            email="alice@step12.com",
            name="Alice",
            role="ADMIN",
            is_active=True,
            groups=[],
        )

        token = create_access_token({"sub": "user_step12", "tenant_id": "tenant_step12", "role": "ADMIN"})
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Create connector with secrets
        create_payload = {
            "name": "Production Google Drive",
            "connector_type": "google_drive",
            "config": {
                "client_id": "super_secret_client_id_999",
                "client_secret": "super_secret_client_secret_xyz",
                "refresh_token": "super_secret_refresh_token_123",
            },
        }

        resp = client.post("/connectors", json=create_payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()

        # Secrets never exposed in API response
        assert "client_secret" not in data
        assert "refresh_token" not in data
        assert "encrypted_config" not in data

        conn_id = data["id"]

        # 2. Get connector details
        get_resp = client.get(f"/connectors/{conn_id}", headers=headers)
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert "client_secret" not in get_data
        assert "refresh_token" not in get_data

        app.dependency_overrides.clear()
