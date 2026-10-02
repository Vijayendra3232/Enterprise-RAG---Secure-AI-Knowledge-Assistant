"""
test_connectors_sync.py — Tests for full and incremental sync, 10-doc lifecycle,
permission-only updates avoiding re-embedding, and clean cascading deletions.
"""

import os
import sys
import json
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.storage.database import Base
from app.storage.models import (
    Tenant,
    User,
    ConnectorConfig,
    Document,
    DocumentPermission,
    DocumentChunk,
)
from app.connectors.secrets import get_secret_provider
from app.connectors.sync import SyncService, SyncMode


@pytest.fixture
def sync_env():
    """Create isolated in-memory DB and test environment."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    # Seed Tenant
    db.add(Tenant(tenant_id="comp_a", name="Company A"))
    db.commit()

    with tempfile.TemporaryDirectory() as source_dir:
        # Create connector config
        secret_provider = get_secret_provider()
        enc_config = secret_provider.encrypt_json({"directory_path": source_dir})
        connector = ConnectorConfig(
            id="conn_sync_test",
            tenant_id="comp_a",
            name="Test Local Source",
            connector_type="local",
            status="ACTIVE",
            encrypted_config=enc_config,
        )
        db.add(connector)
        db.commit()

        # Mock RAG service
        mock_rag = MagicMock()
        mock_rag.vector_db.vectordb.add_documents.return_value = ["chunk_id_1"]
        mock_rag.vector_db.vectordb.delete.return_value = None
        mock_rag.retrieval_pipeline.rebuild_bm25.return_value = None

        yield {
            "db": db,
            "source_dir": source_dir,
            "connector_id": "conn_sync_test",
            "tenant_id": "comp_a",
            "rag_service": mock_rag,
        }

    db.close()


def test_ten_document_incremental_lifecycle(sync_env):
    """
    Requirement 35/40:
    Start with 10 documents -> Initial full sync: 10 imported.
    Then: 2 modified, 1 added, 1 deleted, 6 unchanged.
    Second sync: 6 skipped, 2 updated, 1 added, 1 deleted.
    """
    db = sync_env["db"]
    source_dir = sync_env["source_dir"]
    connector_id = sync_env["connector_id"]
    tenant_id = sync_env["tenant_id"]
    rag_service = sync_env["rag_service"]

    sync_service = SyncService(db)

    # 1. Create 10 initial documents
    for i in range(1, 11):
        filepath = os.path.join(source_dir, f"doc_{i:02d}.txt")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"Content for document {i:02d} initial version.")
        acl_path = f"{filepath}.acl.json"
        with open(acl_path, "w", encoding="utf-8") as f:
            json.dump({
                "permissions": [{"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}],
                "permission_status": "KNOWN"
            }, f)

    # First Sync (FULL)
    res1 = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL, rag_service=rag_service)
    assert res1.status == "SUCCESS"
    assert res1.documents_seen == 10
    assert res1.documents_added == 10
    assert res1.documents_updated == 0
    assert res1.documents_deleted == 0
    assert res1.documents_skipped == 0
    assert res1.permissions_updated == 0

    # 2. Mutate files:
    # - 2 modified: doc_01, doc_02
    with open(os.path.join(source_dir, "doc_01.txt"), "w", encoding="utf-8") as f:
        f.write("Content for document 01 MODIFIED version with new text.")
    with open(os.path.join(source_dir, "doc_02.txt"), "w", encoding="utf-8") as f:
        f.write("Content for document 02 MODIFIED version with updated facts.")

    # - 1 added: doc_11
    with open(os.path.join(source_dir, "doc_11.txt"), "w", encoding="utf-8") as f:
        f.write("Content for brand new document 11.")
    with open(os.path.join(source_dir, "doc_11.txt.acl.json"), "w", encoding="utf-8") as f:
        json.dump({
            "permissions": [{"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}],
            "permission_status": "KNOWN"
        }, f)

    # - 1 deleted: doc_10
    os.remove(os.path.join(source_dir, "doc_10.txt"))
    if os.path.exists(os.path.join(source_dir, "doc_10.txt.acl.json")):
        os.remove(os.path.join(source_dir, "doc_10.txt.acl.json"))

    # - 6 unchanged: doc_03, doc_04, doc_05, doc_06, doc_07, doc_08, doc_09
    # (Note: total is 6 unchanged: docs 3,4,5,6,7,8,9 is 7? 10 initial - 1 deleted = 9; 9 - 2 modified = 7 unchanged. Total seen = 10)

    # Second Sync (FULL)
    res2 = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL, rag_service=rag_service)
    assert res2.status == "SUCCESS"
    assert res2.documents_seen == 10  # 1,2,3,4,5,6,7,8,9,11
    assert res2.documents_added == 1  # doc_11
    assert res2.documents_updated == 2  # doc_01, doc_02
    assert res2.documents_deleted == 1  # doc_10
    assert res2.documents_skipped == 7  # doc_03 to doc_09
    assert res2.errors == []


def test_permission_only_sync_avoids_reembedding(sync_env):
    """
    Requirement 17, 18, 36:
    When document content is unchanged but ACL is modified:
    - Update permissions in PostgreSQL and chunk metadata.
    - DO NOT re-run document chunking or vector embeddings.
    """
    db = sync_env["db"]
    source_dir = sync_env["source_dir"]
    connector_id = sync_env["connector_id"]
    tenant_id = sync_env["tenant_id"]
    rag_service = sync_env["rag_service"]
    sync_service = SyncService(db)

    # 1. Create document with Initial ACL: ENGINEERING = ALLOW
    filepath = os.path.join(source_dir, "sec_doc.txt")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("Highly confidential project roadmap and specifications.")
    acl_path = f"{filepath}.acl.json"
    with open(acl_path, "w", encoding="utf-8") as f:
        json.dump({
            "permissions": [
                {"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "ALLOW"}
            ],
            "permission_status": "KNOWN"
        }, f)

    # Initial sync
    res1 = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL, rag_service=rag_service)
    assert res1.documents_added == 1

    doc = db.query(Document).filter(Document.tenant_id == tenant_id).first()
    assert doc is not None
    initial_version = doc.version
    initial_hash = doc.content_hash

    # Reset mock call count
    rag_service.vector_db.vectordb.add_documents.reset_mock()

    # 2. Modify ACL ONLY: FINANCE = ALLOW, ENGINEERING = DENY
    with open(acl_path, "w", encoding="utf-8") as f:
        json.dump({
            "permissions": [
                {"principal_type": "ROLE", "principal_id": "FINANCE", "effect": "ALLOW"},
                {"principal_type": "ROLE", "principal_id": "ENGINEERING", "effect": "DENY"}
            ],
            "permission_status": "KNOWN"
        }, f)

    # Second sync
    with patch("app.connectors.sync.service.ingest_document") as mock_ingest:
        res2 = sync_service.sync(connector_id, tenant_id, mode=SyncMode.FULL, rag_service=rag_service)

        assert res2.status == "SUCCESS"
        assert res2.permissions_updated == 1
        assert res2.documents_added == 0
        assert res2.documents_updated == 0
        assert res2.documents_skipped == 0

        # CRITICAL ASSERTION: Ingestion / Chunking pipeline was NEVER called!
        mock_ingest.assert_not_called()

        # CRITICAL ASSERTION: Vector DB embedding was NEVER re-added!
        rag_service.vector_db.vectordb.add_documents.assert_not_called()

    # Verify updated DB state
    db.refresh(doc)
    assert doc.content_hash == initial_hash  # Hash unchanged
    assert doc.version == initial_version + 1  # Version bumped
    assert "FINANCE" in doc.metadata_json.get("allowed_roles", []) or doc.access_level == "ROLE"
    assert "ENGINEERING" in doc.metadata_json.get("denied_roles", [])

    # Verify chunk metadata updated
    chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.document_id).all()
    for c in chunks:
        assert "ENGINEERING" in c.metadata_json.get("denied_roles", [])
        assert "FINANCE" in c.metadata_json.get("allowed_roles", [])
