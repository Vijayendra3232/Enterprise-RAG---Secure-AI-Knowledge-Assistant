"""
test_storage_tenant_isolation.py — Direct database and API tests verifying multi-tenant isolation.
Ensures that Company A queries and operations cannot access Company B records under any circumstance.
"""

import os
import sys
import io
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock

# Ensure backend directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.main import app
from app.storage.database import Base, get_db
from app.storage.models import Tenant, User as DBUser, Document as DBDocument, DocumentChunk as DBDocumentChunk, AuditEvent
from app.storage.repositories import SQLDocumentRepository, SQLChunkRepository, SQLUserRepository, SQLAuditRepository
from app.auth.jwt import create_access_token
from app.auth.password import hash_password


@pytest.fixture
def multi_tenant_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()

    # Seed Company A and Company B
    session.add(Tenant(tenant_id="company_a", name="Company A"))
    session.add(Tenant(tenant_id="company_b", name="Company B"))

    # Seed Users
    session.add(
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
    session.add(
        DBUser(
            user_id="admin_b",
            tenant_id="company_b",
            email="admin@companyb.com",
            name="Admin B",
            role="ADMIN",
            password_hash=hash_password("admin123"),
            is_active=True,
        )
    )

    # Seed Company A document and chunk
    session.add(
        DBDocument(
            document_id="doc_a_1",
            tenant_id="company_a",
            filename="confidential_a.txt",
            source="upload",
            content_hash="hash_a",
            access_level="PRIVATE",
            status="INDEXED",
        )
    )
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_a_1",
            document_id="doc_a_1",
            tenant_id="company_a",
            chunk_index=0,
            content_hash="chash_a",
            content="Company A Secret Strategy.",
            metadata_json={"tenant_id": "company_a"},
        )
    )

    # Seed Company B document and chunk
    session.add(
        DBDocument(
            document_id="doc_b_1",
            tenant_id="company_b",
            filename="confidential_b.txt",
            source="upload",
            content_hash="hash_b",
            access_level="PRIVATE",
            status="INDEXED",
        )
    )
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_b_1",
            document_id="doc_b_1",
            tenant_id="company_b",
            chunk_index=0,
            content_hash="chash_b",
            content="Company B Secret Strategy.",
            metadata_json={"tenant_id": "company_b"},
        )
    )

    # Seed Audit Events
    session.add(
        AuditEvent(
            event_id="evt_a",
            tenant_id="company_a",
            user_id="admin_a",
            event_type="LOGIN_SUCCESS",
            action="login",
            result="success",
            details_json={},
        )
    )
    session.add(
        AuditEvent(
            event_id="evt_b",
            tenant_id="company_b",
            user_id="admin_b",
            event_type="LOGIN_SUCCESS",
            action="login",
            result="success",
            details_json={},
        )
    )

    session.commit()
    yield session, TestingSession
    session.close()


def test_repository_tenant_isolation(multi_tenant_db):
    session, _ = multi_tenant_db

    doc_repo = SQLDocumentRepository(session)
    chunk_repo = SQLChunkRepository(session)
    user_repo = SQLUserRepository(session)
    audit_repo = SQLAuditRepository(session)

    # Document isolation: Company A query for Company B document ID returns None
    assert doc_repo.get_by_id("doc_b_1", tenant_id="company_a") is None
    assert doc_repo.get_by_id("doc_a_1", tenant_id="company_a") is not None

    # Document list isolation
    docs_a = doc_repo.list_by_tenant("company_a")
    assert len(docs_a) == 1
    assert docs_a[0].document_id == "doc_a_1"

    # Chunk isolation
    chunks_a = chunk_repo.get_by_tenant("company_a")
    assert len(chunks_a) == 1
    assert chunks_a[0].chunk_id == "chunk_a_1"

    assert chunk_repo.get_by_document("doc_b_1", tenant_id="company_a") == []

    # User isolation
    users_a = user_repo.list_by_tenant("company_a")
    assert len(users_a) == 1
    assert users_a[0].user_id == "admin_a"

    # Audit isolation
    audit_a = audit_repo.query_events(tenant_id="company_a")
    assert len(audit_a) == 1
    assert audit_a[0].event_id == "evt_a"


def test_api_tenant_isolation(multi_tenant_db):
    _, TestingSession = multi_tenant_db

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    mock_rag = MagicMock()
    app.state.rag_service = mock_rag
    client = TestClient(app)

    # Company A token attempting to access Company B document
    token_a = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # GET /documents/doc_b_1 should return 404
    resp = client.get("/documents/doc_b_1", headers=headers_a)
    assert resp.status_code == 404

    # DELETE /documents/doc_b_1 should return 404 (does not reveal existence)
    del_resp = client.delete("/documents/doc_b_1", headers=headers_a)
    assert del_resp.status_code == 404

    # PATCH /documents/doc_b_1/permissions should return 404
    patch_resp = client.patch(
        "/documents/doc_b_1/permissions",
        json={"access_level": "PUBLIC"},
        headers=headers_a,
    )
    assert patch_resp.status_code == 404

    app.dependency_overrides.clear()
