"""
test_storage_idempotency_concurrency.py — Tests for content-hash idempotency, source deduplication,
permission-only updates, and optimistic concurrency control.
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
from app.storage.models import Tenant, User as DBUser, Document as DBDocument
from app.auth.jwt import create_access_token
from app.auth.password import hash_password


@pytest.fixture
def isolated_client():
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
    db.add(
        DBUser(
            user_id="user_a",
            tenant_id="company_a",
            email="usera@companya.com",
            name="User A",
            role="ENGINEERING",
            password_hash=hash_password("password123"),
            is_active=True,
        )
    )
    db.commit()
    db.close()

    # Mock RAG service on app state
    mock_rag = MagicMock()
    mock_rag.vector_db.vectordb.add_documents.return_value = ["chunk_1"]
    mock_rag.vector_db.vectordb.delete.return_value = None
    mock_rag.retrieval_pipeline.rebuild_bm25.return_value = None
    app.state.rag_service = mock_rag

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_idempotent_duplicate_upload(isolated_client):
    token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token}"}

    file_content = b"Engineering Architecture Roadmap 2026."
    files = {"file": ("roadmap.txt", io.BytesIO(file_content), "text/plain")}

    # First upload
    resp1 = isolated_client.post(
        "/documents/upload",
        files=files,
        data={"access_level": "ROLE_BASED", "allowed_roles": "ENGINEERING"},
        headers=headers,
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    doc_id = data1["document_id"]
    assert data1["status"] == "success"

    # Second upload of identical file (idempotent)
    files2 = {"file": ("roadmap.txt", io.BytesIO(file_content), "text/plain")}
    resp2 = isolated_client.post(
        "/documents/upload",
        files=files2,
        data={"access_level": "ROLE_BASED", "allowed_roles": "ENGINEERING"},
        headers=headers,
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["document_id"] == doc_id
    assert "already indexed" in data2["message"].lower() or data2["status"] == "success"


def test_permission_only_update_and_optimistic_concurrency(isolated_client):
    token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token}"}

    file_content = b"Confidential Financial Audit 2026."
    files = {"file": ("audit.txt", io.BytesIO(file_content), "text/plain")}

    resp = isolated_client.post(
        "/documents/upload",
        files=files,
        data={"access_level": "PRIVATE"},
        headers=headers,
    )
    assert resp.status_code == 200
    doc_id = resp.json()["document_id"]

    # 1. Fetch document details
    detail_resp = isolated_client.get(f"/documents/{doc_id}", headers=headers)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    initial_version = detail["version"]
    assert detail["access_level"] == "PRIVATE"

    # 2. Permission-only update with correct expected_version
    patch_resp = isolated_client.patch(
        f"/documents/{doc_id}/permissions",
        json={
            "access_level": "ROLE_BASED",
            "allowed_roles": ["FINANCE", "EXECUTIVE"],
            "expected_version": initial_version,
        },
        headers=headers,
    )
    assert patch_resp.status_code == 200
    updated_detail = patch_resp.json()
    assert updated_detail["access_level"] == "ROLE_BASED"
    assert "FINANCE" in updated_detail["allowed_roles"]
    assert updated_detail["version"] == initial_version + 1

    # 3. Optimistic Concurrency Conflict: passing stale version returns 409 Conflict
    conflict_resp = isolated_client.patch(
        f"/documents/{doc_id}/permissions",
        json={
            "access_level": "PUBLIC",
            "expected_version": initial_version,  # Stale version
        },
        headers=headers,
    )
    assert conflict_resp.status_code == 409
    assert "optimistic concurrency conflict" in conflict_resp.json()["detail"].lower()


def test_source_idempotency_with_changed_content_hash(isolated_client):
    """
    Test composite uniqueness (tenant_id, source, source_document_id):
    - Same source + same content hash -> skip re-indexing (idempotent)
    - Same source + changed content hash -> reprocess, update content_hash & version
    """
    token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token}"}

    v1_content = b"Source document content v1 from Google Drive connector."
    files_v1 = {"file": ("cloud_doc.txt", io.BytesIO(v1_content), "text/plain")}
    data_common = {
        "access_level": "ROLE_BASED",
        "allowed_roles": "ENGINEERING",
        "source": "google_drive",
        "source_document_id": "gdrive_file_999",
    }

    # 1. Initial upload
    resp1 = isolated_client.post(
        "/documents/upload",
        files=files_v1,
        data=data_common,
        headers=headers,
    )
    assert resp1.status_code == 200
    doc_id = resp1.json()["document_id"]

    detail1 = isolated_client.get(f"/documents/{doc_id}", headers=headers).json()
    initial_version = detail1["version"]
    assert detail1["source"] == "google_drive"
    assert detail1["source_document_id"] == "gdrive_file_999"
    assert detail1["status"] == "INDEXED"
    hash1 = detail1["content_hash"]

    # 2. Duplicate upload with same content -> Idempotent skip
    files_v1_dup = {"file": ("cloud_doc.txt", io.BytesIO(v1_content), "text/plain")}
    resp_dup = isolated_client.post(
        "/documents/upload",
        files=files_v1_dup,
        data=data_common,
        headers=headers,
    )
    assert resp_dup.status_code == 200
    assert "already indexed" in resp_dup.json()["message"].lower()

    # 3. Upload with SAME source & source_document_id but CHANGED content
    v2_content = b"Source document content v2 with updated architecture diagrams."
    files_v2 = {"file": ("cloud_doc.txt", io.BytesIO(v2_content), "text/plain")}
    resp2 = isolated_client.post(
        "/documents/upload",
        files=files_v2,
        data=data_common,
        headers=headers,
    )
    assert resp2.status_code == 200
    assert "ingested successfully" in resp2.json()["message"].lower()

    # Verify updated record
    detail2 = isolated_client.get(f"/documents/{doc_id}", headers=headers).json()
    assert detail2["version"] > initial_version
    assert detail2["content_hash"] != hash1
    assert detail2["status"] == "INDEXED"

