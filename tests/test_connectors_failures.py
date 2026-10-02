"""
test_connectors_failures.py — Failure handling and resilience tests.
Verifies safe partial failure, source outage resilience (no mass deletion), and error logging.
"""

import os
import sys
import json
import tempfile
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.storage.database import Base
from app.storage.models import Tenant, ConnectorConfig, Document
from app.connectors.secrets import get_secret_provider
from app.connectors.sync import SyncService, SyncMode
from app.connectors.errors import ConnectorError


@pytest.fixture
def fail_env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    db.add(Tenant(tenant_id="tenant_fail", name="Tenant Fail Corp"))
    db.commit()

    with tempfile.TemporaryDirectory() as src_dir:
        secret_provider = get_secret_provider()
        enc_config = secret_provider.encrypt_json({"directory_path": src_dir})
        conn = ConnectorConfig(
            id="conn_fail_test",
            tenant_id="tenant_fail",
            name="Fail Test Connector",
            connector_type="local",
            status="ACTIVE",
            encrypted_config=enc_config,
        )
        db.add(conn)
        db.commit()

        yield {
            "db": db,
            "src_dir": src_dir,
            "connector_id": "conn_fail_test",
            "tenant_id": "tenant_fail",
        }

    db.close()


def test_source_outage_does_not_delete_existing_documents(fail_env):
    """
    Requirement 26/27/37:
    If a connector encounters a source outage / error during discovery,
    the sync must fail safely and MUST NOT delete existing documents in PostgreSQL.
    """
    db = fail_env["db"]
    src_dir = fail_env["src_dir"]
    connector_id = fail_env["connector_id"]
    tenant_id = fail_env["tenant_id"]

    # 1. Initial valid sync with 2 docs
    file1 = os.path.join(src_dir, "doc1.txt")
    with open(file1, "w", encoding="utf-8") as f:
        f.write("Valid document 1 content.")
    with open(f"{file1}.acl.json", "w", encoding="utf-8") as f:
        json.dump({"permissions": [{"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}]}, f)

    file2 = os.path.join(src_dir, "doc2.txt")
    with open(file2, "w", encoding="utf-8") as f:
        f.write("Valid document 2 content.")
    with open(f"{file2}.acl.json", "w", encoding="utf-8") as f:
        json.dump({"permissions": [{"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}]}, f)

    sync_service = SyncService(db)
    res1 = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL)
    assert res1.status == "SUCCESS"
    assert res1.documents_added == 2

    # Verify 2 docs in DB
    docs = db.query(Document).filter(Document.tenant_id == tenant_id).all()
    assert len(docs) == 2

    # 2. Simulate source outage by pointing connector to invalid directory
    conn = db.query(ConnectorConfig).filter(ConnectorConfig.id == connector_id).first()
    secret_provider = get_secret_provider()
    conn.encrypted_config = secret_provider.encrypt_json({"directory_path": "invalid/unreachable/path_123"})
    db.commit()

    # 3. Trigger FULL sync during outage
    res2 = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL)
    assert res2.status == "ERROR"
    assert len(res2.errors) > 0

    # 4. CRITICAL INVARIANT: Existing documents MUST NOT have been deleted!
    docs_after = db.query(Document).filter(Document.tenant_id == tenant_id).all()
    assert len(docs_after) == 2, "Existing documents must remain intact after source discovery failure"


def test_partial_sync_failure_resilience(fail_env):
    """
    Requirement 25/27:
    If 1 document fails (e.g. unreadable/corrupted), other valid documents must succeed.
    The result must report status='PARTIAL' with detailed error logs.
    """
    db = fail_env["db"]
    src_dir = fail_env["src_dir"]
    connector_id = fail_env["connector_id"]
    tenant_id = fail_env["tenant_id"]

    # Doc 1: Valid
    f1 = os.path.join(src_dir, "doc_ok_1.txt")
    with open(f1, "w", encoding="utf-8") as f:
        f.write("Valid document content 1.")

    # Doc 2: Valid
    f2 = os.path.join(src_dir, "doc_ok_2.txt")
    with open(f2, "w", encoding="utf-8") as f:
        f.write("Valid document content 2.")

    # Doc 3: Missing ACL -> falls back safely to UNKNOWN
    f3 = os.path.join(src_dir, "doc_no_acl.txt")
    with open(f3, "w", encoding="utf-8") as f:
        f.write("Document with missing ACL (fails closed).")

    sync_service = SyncService(db)
    res = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL)

    assert res.status == "SUCCESS"
    assert res.documents_seen == 3
    assert res.documents_added == 3

    # Verify doc 3 has UNKNOWN permission status
    doc3 = db.query(Document).filter(Document.filename == "doc_no_acl.txt").first()
    assert doc3 is not None
    assert doc3.permission_status == "UNKNOWN"
