"""
test_storage_ingestion_lifecycle.py — Tests for document ingestion state machine and cascading deletion.
Verifies states: PENDING -> PROCESSING -> INDEXING -> INDEXED, and FAILED recording on indexing errors.
"""

import os
import sys
import io
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure backend directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.main import app
from app.storage.database import Base, get_db
from app.storage.models import Tenant, User as DBUser, Document as DBDocument, DocumentChunk as DBDocumentChunk
from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from app.storage.blob import get_document_storage


@pytest.fixture
def lifecycle_client():
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

    # Seed test tenant and users
    db = TestingSession()
    db.add(Tenant(tenant_id="company_a", name="Company A"))
    db.add(
        DBUser(
            user_id="admin_a",
            tenant_id="company_a",
            email="admin@companya.com",
            name="Admin A",
            role="ADMIN",
            password_hash=hash_password("admin123"),
            is_active=True,
        )
    )
    db.commit()
    db.close()

    mock_rag = MagicMock()
    mock_rag.vector_db.vectordb.add_documents.return_value = ["chunk_1"]
    mock_rag.vector_db.vectordb.delete.return_value = None
    mock_rag.retrieval_pipeline.rebuild_bm25.return_value = None
    app.state.rag_service = mock_rag

    client = TestClient(app)
    yield client, TestingSession
    app.dependency_overrides.clear()


def test_successful_ingestion_lifecycle(lifecycle_client):
    client, SessionMaker = lifecycle_client
    token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token}"}

    file_content = b"Enterprise Security Standard Operating Procedure 2026."
    files = {"file": ("sop.txt", io.BytesIO(file_content), "text/plain")}

    resp = client.post("/documents/upload", files=files, headers=headers)
    assert resp.status_code == 200
    doc_id = resp.json()["document_id"]

    # Verify database state is INDEXED with chunks and storage path
    db = SessionMaker()
    db_doc = db.query(DBDocument).filter(DBDocument.document_id == doc_id).first()
    assert db_doc is not None
    assert db_doc.status == "INDEXED"
    assert db_doc.indexed_at is not None
    assert db_doc.storage_path is not None

    chunks = db.query(DBDocumentChunk).filter(DBDocumentChunk.document_id == doc_id).all()
    assert len(chunks) > 0
    db.close()


def test_failed_ingestion_state_recording(lifecycle_client):
    client, SessionMaker = lifecycle_client
    token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token}"}

    # Simulate vector store failure during upload
    app.state.rag_service.vector_db.vectordb.add_documents.side_effect = RuntimeError("Vector DB connection timeout")

    file_content = b"Quarterly Earnings Summary 2026."
    files = {"file": ("earnings.txt", io.BytesIO(file_content), "text/plain")}

    resp = client.post("/documents/upload", files=files, headers=headers)
    assert resp.status_code == 500

    # Reset mock side effect
    app.state.rag_service.vector_db.vectordb.add_documents.side_effect = None

    # Verify database state was recorded as FAILED and NOT INDEXED
    db = SessionMaker()
    failed_docs = db.query(DBDocument).filter(DBDocument.status == "FAILED").all()
    assert len(failed_docs) >= 1
    assert failed_docs[0].status == "FAILED"
    assert "Vector DB connection timeout" in failed_docs[0].error_message
    db.close()


def test_clean_cascading_deletion(lifecycle_client):
    client, SessionMaker = lifecycle_client
    token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token}"}

    file_content = b"Temporary Project Specifications."
    files = {"file": ("temp_spec.txt", io.BytesIO(file_content), "text/plain")}

    resp = client.post("/documents/upload", files=files, headers=headers)
    assert resp.status_code == 200
    doc_id = resp.json()["document_id"]

    db = SessionMaker()
    doc_before = db.query(DBDocument).filter(DBDocument.document_id == doc_id).first()
    storage_path = doc_before.storage_path
    blob_storage = get_document_storage()
    assert blob_storage.exists(storage_path) is True
    db.close()

    # Perform cascading delete
    del_resp = client.delete(f"/documents/{doc_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # Verify DB records, chunks, and storage file are completely removed
    db = SessionMaker()
    doc_after = db.query(DBDocument).filter(DBDocument.document_id == doc_id).first()
    assert doc_after is None
    chunks_after = db.query(DBDocumentChunk).filter(DBDocumentChunk.document_id == doc_id).all()
    assert len(chunks_after) == 0
    assert blob_storage.exists(storage_path) is False
    db.close()
