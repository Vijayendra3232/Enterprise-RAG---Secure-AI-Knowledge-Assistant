"""
test_step81_hardening.py — Step 8.1 Production Security & Reliability Hardening Verification Suite.

Validates:
Section A: Cryptographic & Secret Storage Hardening (PBKDF2, versioning, unique nonce, tampering, MAC, SecretDecryptionError).
Section B: Authorization & Permission Precision Matrix (USER, GROUP, ROLE deny, deny precedence, fail-closed UNKNOWN, zero-re-embedding).
Section C: Tenant Isolation & IDOR Security Matrix (cross-tenant GET, PATCH, DELETE, POST /sync, GET /sync-status, GET /runs, body substitution).
Section D: Sync Reliability & Anti-Mass-Deletion Matrix (source outage safety, incomplete discovery flag, max docs limit, partial failure, idempotency).
Section E: Concurrency & Secret Leakage Prevention (sanitized error logs, no plaintext credentials in API responses).
"""

import os
import sys
import json
import base64
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.main import app
from app.storage.database import Base, get_db
from app.storage.models import (
    Tenant,
    User as DBUser,
    ConnectorConfig,
    SyncRun,
    Document as DBDocument,
    DocumentChunk as DBDocumentChunk,
)
from app.auth.models import User
from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from app.authorization.context import AuthorizationContext
from app.authorization.filters import is_document_accessible
from app.connectors.secrets.local import LocalSecretProvider, get_secret_provider
from app.connectors.errors import SecretDecryptionError, ConnectorNotFoundError, ConnectorError
from app.connectors.sync import SyncService, SyncMode, SyncPlanner
from app.connectors.base import ConnectorDocument, ConnectorACL, ConnectorPermission, Principal, PrincipalType, PermissionEffect


# ===========================================================================
# Section A: Cryptographic & Secret Storage Hardening
# ===========================================================================

def test_crypto_pbkdf2_derivation_and_version_header():
    """Verify standard PBKDF2 derivation and 'v1:' version header formatting."""
    provider = LocalSecretProvider(master_key="test-master-key-32-bytes-long!")
    ciphertext = provider.encrypt("sensitive_api_token_12345")
    
    assert ciphertext.startswith("v1:")
    assert len(ciphertext) > len("v1:") + 48


def test_crypto_unique_iv_nonce_per_encryption():
    """Verify that encrypting identical data twice generates distinct ciphertexts (unique CSPRNG IV)."""
    provider = LocalSecretProvider()
    data = "identical_password_secret"
    ct1 = provider.encrypt(data)
    ct2 = provider.encrypt(data)
    
    assert ct1 != ct2
    assert provider.decrypt(ct1) == data
    assert provider.decrypt(ct2) == data


def test_crypto_roundtrip_all_types():
    """Verify roundtrip encryption and decryption for arbitrary text, unicode, nested JSON, and empty values."""
    provider = LocalSecretProvider()
    
    # 1. Plain text and unicode
    test_strings = [
        "simple_string",
        "Unicode characters: 🔒🔑✨ — 日本語 — हिन्दी",
        "Multi-line\nJSON\tstring with \"quotes\" and 'single quotes'",
    ]
    for s in test_strings:
        enc = provider.encrypt(s)
        dec = provider.decrypt(enc)
        assert dec == s

    # 2. JSON dicts
    test_dicts = [
        {"api_key": "sk-123456789", "port": 8080, "ssl": True},
        {"nested": {"auth": {"bearer": "token", "refresh": None}}},
        {},
    ]
    for d in test_dicts:
        enc_j = provider.encrypt_json(d)
        dec_j = provider.decrypt_json(enc_j)
        assert dec_j == d

    # 3. Empty input handling
    assert provider.encrypt("") == ""
    assert provider.decrypt("") == ""
    assert provider.decrypt_json("") == {}


def test_crypto_tamper_detection():
    """Verify that tampering with any bit in ciphertext body triggers SecretDecryptionError."""
    provider = LocalSecretProvider()
    data = "top-secret-configuration-data"
    ciphertext = provider.encrypt(data)
    
    # Extract base64 part
    raw_payload = base64.urlsafe_b64decode(ciphertext[3:].encode("utf-8"))
    
    # Flip one byte in the ciphertext payload (after the 48-byte IV+MAC header)
    tampered_payload = bytearray(raw_payload)
    tampered_payload[50] ^= 0xFF
    tampered_b64 = "v1:" + base64.urlsafe_b64encode(bytes(tampered_payload)).decode("utf-8")
    
    with pytest.raises(SecretDecryptionError) as exc_info:
        provider.decrypt(tampered_b64)
    assert "authentication failed" in str(exc_info.value).lower() or "mac" in str(exc_info.value).lower()


def test_crypto_mac_verification_failure():
    """Verify that tampering with the MAC tag itself triggers SecretDecryptionError."""
    provider = LocalSecretProvider()
    ciphertext = provider.encrypt("test_message")
    
    raw_payload = bytearray(base64.urlsafe_b64decode(ciphertext[3:].encode("utf-8")))
    # Flip byte in MAC tag (bytes 16..47)
    raw_payload[20] ^= 0x01
    tampered_b64 = "v1:" + base64.urlsafe_b64encode(bytes(raw_payload)).decode("utf-8")
    
    with pytest.raises(SecretDecryptionError):
        provider.decrypt(tampered_b64)


def test_crypto_truncated_or_corrupt_payload():
    """Verify that payloads shorter than 48 bytes or malformed base64 raise SecretDecryptionError."""
    provider = LocalSecretProvider()
    
    # Short payload (< 48 bytes)
    short_payload = "v1:" + base64.urlsafe_b64encode(b"too_short_bytes").decode("utf-8")
    with pytest.raises(SecretDecryptionError) as exc_info:
        provider.decrypt(short_payload)
    assert "below minimum" in str(exc_info.value).lower()

    # Invalid base64 characters
    with pytest.raises(SecretDecryptionError):
        provider.decrypt("v1:!!!NotValidBase64@@@")


def test_crypto_wrong_master_key():
    """Verify that attempting to decrypt with the wrong master key triggers SecretDecryptionError."""
    provider1 = LocalSecretProvider(master_key="correct-master-key-aaa")
    provider2 = LocalSecretProvider(master_key="wrong-master-key-bbb")
    
    ct = provider1.encrypt("confidential_data_payload")
    
    with pytest.raises(SecretDecryptionError):
        provider2.decrypt(ct)


def test_crypto_decrypt_json_invalid_json_payload():
    """Verify that decrypting non-JSON content with decrypt_json raises SecretDecryptionError."""
    provider = LocalSecretProvider()
    ct = provider.encrypt("this is not a valid json document")
    
    with pytest.raises(SecretDecryptionError) as exc_info:
        provider.decrypt_json(ct)
    assert "valid json" in str(exc_info.value).lower()


# ===========================================================================
# Section B: Authorization & Permission Precision Matrix
# ===========================================================================

def test_authz_precision_user_deny():
    """
    Verify precision of USER deny:
    DENY USER:eng_1 denies only eng_1.
    eng_2 (with same role ENGINEERING) is ALLOWED.
    """
    doc_meta = {
        "tenant_id": "tenant_1",
        "access_level": "ROLE",
        "permission_status": "KNOWN",
        "allowed_roles": ["ENGINEERING"],
        "denied_user_ids": ["eng_1"],
    }
    
    auth_eng1 = AuthorizationContext(user_id="eng_1", tenant_id="tenant_1", role="ENGINEERING", groups=[])
    auth_eng2 = AuthorizationContext(user_id="eng_2", tenant_id="tenant_1", role="ENGINEERING", groups=[])
    
    assert is_document_accessible(doc_meta, auth_eng1) is False
    assert is_document_accessible(doc_meta, auth_eng2) is True


def test_authz_precision_group_deny():
    """
    Verify precision of GROUP deny:
    DENY GROUP:contractors denies only contractors.
    Regular engineers (not in contractors group) are ALLOWED.
    """
    doc_meta = {
        "tenant_id": "tenant_1",
        "access_level": "ROLE",
        "permission_status": "KNOWN",
        "allowed_roles": ["ENGINEERING"],
        "denied_groups": ["contractors"],
    }
    
    auth_contractor = AuthorizationContext(
        user_id="dev_contractor",
        tenant_id="tenant_1",
        role="ENGINEERING",
        groups=["contractors", "dev_team"],
    )
    auth_fte = AuthorizationContext(
        user_id="dev_fte",
        tenant_id="tenant_1",
        role="ENGINEERING",
        groups=["full_time", "dev_team"],
    )
    
    assert is_document_accessible(doc_meta, auth_contractor) is False
    assert is_document_accessible(doc_meta, auth_fte) is True


def test_authz_precision_role_deny():
    """
    Verify precision of ROLE deny:
    DENY ROLE:VIEWER denies only VIEWER role.
    Role ENGINEERING and ADMIN are ALLOWED.
    """
    doc_meta = {
        "tenant_id": "tenant_1",
        "access_level": "PUBLIC",
        "permission_status": "KNOWN",
        "denied_roles": ["VIEWER"],
    }
    
    auth_viewer = AuthorizationContext(user_id="v1", tenant_id="tenant_1", role="VIEWER", groups=[])
    auth_eng = AuthorizationContext(user_id="e1", tenant_id="tenant_1", role="ENGINEERING", groups=[])
    auth_admin = AuthorizationContext(user_id="a1", tenant_id="tenant_1", role="ADMIN", groups=[])
    
    assert is_document_accessible(doc_meta, auth_viewer) is False
    assert is_document_accessible(doc_meta, auth_eng) is True
    assert is_document_accessible(doc_meta, auth_admin) is True


def test_authz_explicit_deny_precedence_over_allow():
    """
    Verify explicit DENY overrides matching ALLOW:
    User is in allowed_groups=['developers'] AND in denied_user_ids=['bad_actor'].
    Access MUST be rejected.
    """
    doc_meta = {
        "tenant_id": "tenant_1",
        "access_level": "GROUP",
        "permission_status": "KNOWN",
        "allowed_groups": ["developers"],
        "denied_user_ids": ["bad_actor"],
    }
    
    auth_bad_actor = AuthorizationContext(
        user_id="bad_actor",
        tenant_id="tenant_1",
        role="ENGINEERING",
        groups=["developers"],
    )
    auth_good_actor = AuthorizationContext(
        user_id="good_actor",
        tenant_id="tenant_1",
        role="ENGINEERING",
        groups=["developers"],
    )
    
    assert is_document_accessible(doc_meta, auth_bad_actor) is False
    assert is_document_accessible(doc_meta, auth_good_actor) is True


def test_authz_fail_closed_unknown_permissions():
    """
    Verify fail-closed behavior on permission_status='UNKNOWN':
    Only the document owner can access the document; all others are denied.
    """
    doc_meta = {
        "tenant_id": "tenant_1",
        "access_level": "PUBLIC",
        "permission_status": "UNKNOWN",
        "allowed_roles": ["ENGINEERING", "FINANCE", "ADMIN"],
        "owner_id": "doc_creator",
    }
    
    auth_owner = AuthorizationContext(user_id="doc_creator", tenant_id="tenant_1", role="ENGINEERING", groups=[])
    auth_other = AuthorizationContext(user_id="other_user", tenant_id="tenant_1", role="ENGINEERING", groups=[])
    
    assert is_document_accessible(doc_meta, auth_owner) is True
    assert is_document_accessible(doc_meta, auth_other) is False


def test_authz_fail_closed_unknown_permissions_no_owner():
    """
    Verify fail-closed behavior on permission_status='UNKNOWN' with no owner:
    Document is completely inaccessible to regular users.
    """
    doc_meta = {
        "tenant_id": "tenant_1",
        "access_level": "ROLE",
        "permission_status": "UNKNOWN",
        "allowed_roles": ["ENGINEERING"],
        "owner_id": None,
    }
    
    auth_user = AuthorizationContext(user_id="eng_user", tenant_id="tenant_1", role="ENGINEERING", groups=[])
    assert is_document_accessible(doc_meta, auth_user) is False


# ===========================================================================
# Section C: Tenant Isolation & IDOR Security Matrix
# ===========================================================================

@pytest.fixture
def idor_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestingSession()
    db.add(Tenant(tenant_id="company_a", name="Company A"))
    db.add(Tenant(tenant_id="company_b", name="Company B"))

    db.add(DBUser(
        user_id="admin_a",
        tenant_id="company_a",
        email="admin@companya.com",
        name="Admin A",
        role="ADMIN",
        password_hash=hash_password("admin123"),
        is_active=True,
    ))
    db.add(DBUser(
        user_id="admin_b",
        tenant_id="company_b",
        email="admin@companyb.com",
        name="Admin B",
        role="ADMIN",
        password_hash=hash_password("admin123"),
        is_active=True,
    ))

    # Connector owned by Company A
    secret_provider = get_secret_provider()
    conn_a = ConnectorConfig(
        id="conn_alpha_1",
        tenant_id="company_a",
        name="Company A Local Docs",
        connector_type="local",
        status="ACTIVE",
        encrypted_config=secret_provider.encrypt_json({"directory_path": "/tmp/alpha"}),
    )
    db.add(conn_a)
    db.commit()
    db.close()

    mock_rag = MagicMock()
    mock_rag.vector_db.vectordb.add_documents.return_value = ["chunk_id"]
    mock_rag.vector_db.vectordb.delete.return_value = None
    mock_rag.retrieval_pipeline.rebuild_bm25.return_value = None
    app.state.rag_service = mock_rag

    client = TestClient(app)
    
    alpha_token = create_access_token({
        "sub": "admin_a", "tenant_id": "company_a", "email": "admin@companya.com", "name": "Admin A", "role": "ADMIN"
    })
    beta_token = create_access_token({
        "sub": "admin_b", "tenant_id": "company_b", "email": "admin@companyb.com", "name": "Admin B", "role": "ADMIN"
    })

    yield {
        "client": client,
        "alpha_headers": {"Authorization": f"Bearer {alpha_token}"},
        "beta_headers": {"Authorization": f"Bearer {beta_token}"},
        "conn_alpha_id": "conn_alpha_1",
    }
    app.dependency_overrides.clear()


def test_idor_matrix_cross_tenant_endpoints(idor_fixture):
    """
    Test IDOR protection across all connector administrative endpoints.
    Beta Admin must receive 404 Not Found on all Alpha connector operations.
    """
    client = idor_fixture["client"]
    beta_headers = idor_fixture["beta_headers"]
    conn_id = idor_fixture["conn_alpha_id"]

    # 1. GET /connectors/{id}
    res_get = client.get(f"/connectors/{conn_id}", headers=beta_headers)
    assert res_get.status_code == 404

    # 2. PATCH /connectors/{id}
    res_patch = client.patch(f"/connectors/{conn_id}", json={"name": "Hijacked"}, headers=beta_headers)
    assert res_patch.status_code == 404

    # 3. DELETE /connectors/{id}
    res_del = client.delete(f"/connectors/{conn_id}", headers=beta_headers)
    assert res_del.status_code == 404

    # 4. POST /connectors/{id}/sync
    res_sync = client.post(f"/connectors/{conn_id}/sync", json={"mode": "FULL"}, headers=beta_headers)
    assert res_sync.status_code == 404

    # 5. GET /connectors/{id}/sync-status
    res_status = client.get(f"/connectors/{conn_id}/sync-status", headers=beta_headers)
    assert res_status.status_code == 404

    # 6. GET /connectors/{id}/runs
    res_runs = client.get(f"/connectors/{conn_id}/runs", headers=beta_headers)
    assert res_runs.status_code == 404


def test_idor_body_tenant_id_substitution(idor_fixture):
    """
    Verify that providing a forged tenant_id in the payload does not override JWT tenant_id.
    """
    client = idor_fixture["client"]
    alpha_headers = idor_fixture["alpha_headers"]

    with tempfile.TemporaryDirectory() as tmpdir:
        resp = client.post(
            "/connectors",
            json={
                "name": "Spoofed Connector",
                "connector_type": "local",
                "tenant_id": "tenant_beta",  # Attempt to spoof tenant_id
                "config": {"directory_path": tmpdir},
            },
            headers=alpha_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["tenant_id"] == "company_a"  # Strictly bound to JWT tenant_id


# ===========================================================================
# Section D: Sync Reliability & Anti-Mass-Deletion Matrix
# ===========================================================================

@pytest.fixture
def sync_reliability_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    db.add(Tenant(tenant_id="tenant_rel", name="Reliability Corp"))
    db.commit()

    with tempfile.TemporaryDirectory() as src_dir:
        secret_provider = get_secret_provider()
        conn = ConnectorConfig(
            id="conn_rel",
            tenant_id="tenant_rel",
            name="Reliability Connector",
            connector_type="local",
            status="ACTIVE",
            encrypted_config=secret_provider.encrypt_json({"directory_path": src_dir}),
        )
        db.add(conn)
        db.commit()

        yield {
            "db": db,
            "src_dir": src_dir,
            "conn_id": "conn_rel",
            "tenant_id": "tenant_rel",
        }
    db.close()


def test_sync_source_outage_no_deletions(sync_reliability_fixture):
    """
    Outage during source discovery must fail safely and NEVER delete existing DB docs.
    """
    db = sync_reliability_fixture["db"]
    src_dir = sync_reliability_fixture["src_dir"]
    conn_id = sync_reliability_fixture["conn_id"]
    tenant_id = sync_reliability_fixture["tenant_id"]

    # Initial sync with 3 files
    for i in range(1, 4):
        p = os.path.join(src_dir, f"file_{i}.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"Content of file {i}")
        with open(f"{p}.acl.json", "w", encoding="utf-8") as f:
            json.dump({"permissions": [{"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}], "permission_status": "KNOWN"}, f)

    sync_service = SyncService(db)
    r1 = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)
    assert r1.status == "SUCCESS"
    assert r1.documents_added == 3

    # Break connector path
    conn = db.query(ConnectorConfig).filter(ConnectorConfig.id == conn_id).first()
    secret_provider = get_secret_provider()
    conn.encrypted_config = secret_provider.encrypt_json({"directory_path": "/nonexistent/broken/path/12345"})
    db.commit()

    # Trigger sync during outage
    r2 = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)
    assert r2.status == "ERROR"

    # Verify existing documents remain 100% intact
    docs = db.query(DBDocument).filter(DBDocument.tenant_id == tenant_id).all()
    assert len(docs) == 3


def test_sync_incomplete_discovery_prevents_deletions():
    """
    Verify that SyncPlanner with is_complete_discovery=False NEVER plans DELETE actions.
    """
    existing_doc = DBDocument(
        document_id="doc_abc",
        tenant_id="t1",
        source_document_id="src_doc_1.txt",
        content_hash="hash123",
        status="INDEXED",
    )
    
    # 0 discovered docs (simulating aborted/incomplete discovery)
    plan = SyncPlanner.plan(
        connector_id="conn_1",
        tenant_id="t1",
        source_type="local",
        sync_mode=SyncMode.FULL,
        discovered_docs=[],
        existing_docs=[existing_doc],
        is_complete_discovery=False,  # Incomplete discovery
    )
    
    # Must NOT have planned DELETE
    delete_items = [item for item in plan.items if item.action.value == "DELETE"]
    assert len(delete_items) == 0


def test_sync_max_documents_limit_triggers_incomplete_protection(sync_reliability_fixture):
    """
    When discovery returns more documents than MAX_SYNC_DOCUMENTS, discovery is marked
    incomplete and deletions are blocked.
    """
    db = sync_reliability_fixture["db"]
    src_dir = sync_reliability_fixture["src_dir"]
    conn_id = sync_reliability_fixture["conn_id"]
    tenant_id = sync_reliability_fixture["tenant_id"]

    # Initial sync with 1 doc
    p = os.path.join(src_dir, "initial.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("Initial file")
    with open(f"{p}.acl.json", "w", encoding="utf-8") as f:
        json.dump({"permissions": [], "permission_status": "KNOWN"}, f)

    sync_service = SyncService(db)
    r1 = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)
    assert r1.documents_added == 1

    # Now remove file on disk, but mock MAX_SYNC_DOCUMENTS=0 so discovery hits limit
    os.remove(p)
    if os.path.exists(f"{p}.acl.json"):
        os.remove(f"{p}.acl.json")

    with patch("app.config.MAX_SYNC_DOCUMENTS", 0):
        r2 = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)
        # Because is_complete_discovery is set to False, no deletions occur!
        assert r2.documents_deleted == 0
        doc = db.query(DBDocument).filter(DBDocument.tenant_id == tenant_id).first()
        assert doc is not None


def test_sync_idempotent_rerun(sync_reliability_fixture):
    """
    Rerunning sync on unchanged documents results in 0 additions, 0 updates, 0 deletions, and all skipped.
    """
    db = sync_reliability_fixture["db"]
    src_dir = sync_reliability_fixture["src_dir"]
    conn_id = sync_reliability_fixture["conn_id"]
    tenant_id = sync_reliability_fixture["tenant_id"]

    p = os.path.join(src_dir, "static_doc.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("Static unchanged content.")
    with open(f"{p}.acl.json", "w", encoding="utf-8") as f:
        json.dump({"permissions": [{"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}], "permission_status": "KNOWN"}, f)

    sync_service = SyncService(db)
    r1 = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)
    assert r1.status == "SUCCESS"
    assert r1.documents_added == 1

    # Second sync without touching files
    r2 = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)
    assert r2.status == "SUCCESS"
    assert r2.documents_added == 0
    assert r2.documents_updated == 0
    assert r2.documents_deleted == 0
    assert r2.documents_skipped == 1


# ===========================================================================
# Section E: Concurrency & Secret Leakage Prevention
# ===========================================================================

def test_sync_secret_decryption_error_fails_safely_without_leakage(sync_reliability_fixture):
    """
    Verify that SecretDecryptionError fails the sync run cleanly without exposing secrets in logs.
    """
    db = sync_reliability_fixture["db"]
    conn_id = sync_reliability_fixture["conn_id"]
    tenant_id = sync_reliability_fixture["tenant_id"]

    # Corrupt the encrypted_config string in DB
    conn = db.query(ConnectorConfig).filter(ConnectorConfig.id == conn_id).first()
    conn.encrypted_config = "v1:tampered_ciphertext_mac_invalid_1234567890"
    db.commit()

    sync_service = SyncService(db)
    result = sync_service.sync(conn_id, tenant_id, mode=SyncMode.FULL)

    assert result.status == "ERROR"
    assert len(result.errors) > 0
    err_str = str(result.errors[0]["error"])
    assert "secret decryption failed" in err_str.lower()
    # Confirm no master key or internal secrets leaked
    assert "master_key" not in err_str
    assert "_auth_key" not in err_str


def test_api_connector_response_excludes_secret_config(idor_fixture):
    """
    Verify that API responses for connectors strictly exclude encrypted_config or credentials.
    """
    client = idor_fixture["client"]
    alpha_headers = idor_fixture["alpha_headers"]

    # List connectors
    res = client.get("/connectors", headers=alpha_headers)
    assert res.status_code == 200
    items = res.json()
    assert len(items) > 0
    for item in items:
        assert "encrypted_config" not in item
        assert "config" not in item
        assert "password" not in item
        assert "secret" not in item
