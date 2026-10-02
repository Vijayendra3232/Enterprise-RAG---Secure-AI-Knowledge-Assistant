"""
test_connectors_api.py — Tests for Connector Administration API endpoints.
Verifies RBAC protection (SETTINGS_MANAGE), CRUD operations, sync triggers, and tenant isolation.
"""

import os
import sys
import tempfile
import pytest
from unittest.mock import MagicMock
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
from app.storage.models import Tenant, User as DBUser
from app.auth.jwt import create_access_token
from app.auth.password import hash_password


@pytest.fixture
def api_client():
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

    # Seed Tenants & Users
    db = TestingSession()
    db.add(Tenant(tenant_id="company_a", name="Company A"))
    db.add(Tenant(tenant_id="company_b", name="Company B"))

    # Admin A
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
    # Non-admin A
    db.add(
        DBUser(
            user_id="user_a",
            tenant_id="company_a",
            email="usera@companya.com",
            name="User A",
            role="ENGINEERING",
            password_hash=hash_password("user123"),
            is_active=True,
        )
    )
    # Admin B
    db.add(
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


def test_connector_crud_and_rbac(api_client):
    admin_token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    user_token = create_access_token({
        "sub": "user_a",
        "tenant_id": "company_a",
        "email": "usera@companya.com",
        "name": "User A",
        "role": "ENGINEERING",
    })
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_headers = {"Authorization": f"Bearer {user_token}"}

    # 1. Non-admin is rejected (403 Forbidden)
    resp_forbidden = api_client.post(
        "/connectors",
        json={"name": "Forbidden Connector", "connector_type": "local", "config": {}},
        headers=user_headers,
    )
    assert resp_forbidden.status_code == 403

    # 2. Admin creates connector
    with tempfile.TemporaryDirectory() as tmpdir:
        resp_create = api_client.post(
            "/connectors",
            json={"name": "Engineering Local Docs", "connector_type": "local", "config": {"directory_path": tmpdir}},
            headers=admin_headers,
        )
        assert resp_create.status_code == 201
        conn_data = resp_create.json()
        conn_id = conn_data["id"]
        assert conn_data["name"] == "Engineering Local Docs"
        assert conn_data["tenant_id"] == "company_a"

        # 3. List connectors
        resp_list = api_client.get("/connectors", headers=admin_headers)
        assert resp_list.status_code == 200
        items = resp_list.json()
        assert len(items) == 1
        assert items[0]["id"] == conn_id

        # 4. Get connector by ID
        resp_get = api_client.get(f"/connectors/{conn_id}", headers=admin_headers)
        assert resp_get.status_code == 200
        assert resp_get.json()["id"] == conn_id

        # 5. Patch connector
        resp_patch = api_client.patch(
            f"/connectors/{conn_id}",
            json={"name": "Updated Engineering Docs", "status": "ACTIVE"},
            headers=admin_headers,
        )
        assert resp_patch.status_code == 200
        assert resp_patch.json()["name"] == "Updated Engineering Docs"

        # 6. Trigger sync
        resp_sync = api_client.post(
            f"/connectors/{conn_id}/sync",
            json={"mode": "FULL"},
            headers=admin_headers,
        )
        assert resp_sync.status_code == 200
        sync_run = resp_sync.json()
        assert sync_run["connector_id"] == conn_id
        assert sync_run["status"] == "SUCCESS"

        # 7. Get sync status & list runs
        resp_status = api_client.get(f"/connectors/{conn_id}/sync-status", headers=admin_headers)
        assert resp_status.status_code == 200
        assert resp_status.json()["id"] == sync_run["id"]

        resp_runs = api_client.get(f"/connectors/{conn_id}/runs", headers=admin_headers)
        assert resp_runs.status_code == 200
        assert len(resp_runs.json()) == 1

        # 8. Delete connector
        resp_del = api_client.delete(f"/connectors/{conn_id}", headers=admin_headers)
        assert resp_del.status_code == 200

        # Verify 404 after deletion
        resp_get_deleted = api_client.get(f"/connectors/{conn_id}", headers=admin_headers)
        assert resp_get_deleted.status_code == 404


def test_cross_tenant_api_isolation(api_client):
    admin_a_token = create_access_token({
        "sub": "admin_a",
        "tenant_id": "company_a",
        "email": "admin@companya.com",
        "name": "Admin A",
        "role": "ADMIN",
    })
    admin_b_token = create_access_token({
        "sub": "admin_b",
        "tenant_id": "company_b",
        "email": "admin@companyb.com",
        "name": "Admin B",
        "role": "ADMIN",
    })
    headers_a = {"Authorization": f"Bearer {admin_a_token}"}
    headers_b = {"Authorization": f"Bearer {admin_b_token}"}

    # Admin A creates a connector in Company A
    with tempfile.TemporaryDirectory() as tmpdir:
        resp_a = api_client.post(
            "/connectors",
            json={"name": "Company A Connector", "connector_type": "local", "config": {"directory_path": tmpdir}},
            headers=headers_a,
        )
        assert resp_a.status_code == 201
        conn_a_id = resp_a.json()["id"]

        # Admin B attempts to access Company A's connector -> 404 Not Found
        resp_cross_get = api_client.get(f"/connectors/{conn_a_id}", headers=headers_b)
        assert resp_cross_get.status_code == 404

        resp_cross_patch = api_client.patch(f"/connectors/{conn_a_id}", json={"name": "Hacked"}, headers=headers_b)
        assert resp_cross_patch.status_code == 404

        resp_cross_sync = api_client.post(f"/connectors/{conn_a_id}/sync", json={"mode": "FULL"}, headers=headers_b)
        assert resp_cross_sync.status_code == 404

        resp_cross_delete = api_client.delete(f"/connectors/{conn_a_id}", headers=headers_b)
        assert resp_cross_delete.status_code == 404
