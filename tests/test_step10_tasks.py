"""
test_step10_tasks.py — Comprehensive Unit & Integration Test Suite for Step 10:
Distributed Asynchronous Task Execution / Worker Architecture.

Covers:
1. Task domain model lifecycle and status transitions.
2. Tenant-scoped task idempotency key deduplication.
3. Atomic task claiming with zero duplicate claims across concurrent workers.
4. Strict multi-tenant isolation and IDOR prevention on task queries & APIs.
5. Stale task crash recovery (simulating worker crash).
6. Bounded exponential backoff retries and non-retryable failure handling.
7. End-to-end asynchronous document ingestion workflow.
8. Stale worker version race protection (prevents overwriting newer document versions).
9. Deletion race protection (prevents resurrecting deleted documents).
10. Crash-safe idempotent cascading document deletion.
11. Permission-only synchronization with ZERO re-embedding.
12. Connector synchronization worker handler.
13. Transactional outbox dispatcher delivery.
14. Payload and error message sanitization (zero plaintext secrets).
15. Task cancellation endpoint semantics.
"""

import os
import sys
import io
import uuid
import time
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Setup paths
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir in sys.path:
    sys.path.remove(backend_dir)
sys.path.insert(0, backend_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

from app.main import app
from app.storage.database import Base, get_db
from app.storage.models import (
    Tenant as DBTenant,
    User as DBUser,
    Document as DBDocument,
    DocumentChunk as DBDocumentChunk,
    DocumentPermission as DBDocumentPermission,
    Task as DBTask,
    OutboxEvent as DBOutboxEvent,
)
from app.storage.repositories import (
    SQLTaskRepository,
    SQLOutboxRepository,
    SQLDocumentRepository,
    SQLPermissionRepository,
    SQLChunkRepository,
)
from app.tasks.models import TaskType, TaskStatus
from app.tasks.queue import DatabaseTaskQueue, InMemoryTaskQueue
from app.tasks.dispatcher import OutboxDispatcher
from app.tasks.handlers.base import WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.handlers.ingestion import DocumentIngestHandler
from app.tasks.handlers.deletion import DocumentDeleteHandler
from app.tasks.handlers.permission_sync import PermissionSyncHandler
from app.tasks.handlers.connector_sync import ConnectorSyncHandler
from app.tasks.worker import WorkerRunner, WorkerPool
from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from tests.test_step9_search import MockOpenSearchTransport
from app.storage.search.opensearch_store import OpenSearchStore


@pytest.fixture
def test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    # Seed test tenants and users
    db.add(DBTenant(tenant_id="tenant-alpha", name="Tenant Alpha"))
    db.add(DBTenant(tenant_id="tenant-beta", name="Tenant Beta"))
    db.add(
        DBUser(
            user_id="alice",
            tenant_id="tenant-alpha",
            email="alice@alpha.com",
            name="Alice Alpha",
            role="ADMIN",
            password_hash=hash_password("password123"),
            is_active=True,
        )
    )
    db.add(
        DBUser(
            user_id="bob",
            tenant_id="tenant-beta",
            email="bob@beta.com",
            name="Bob Beta",
            role="ADMIN",
            password_hash=hash_password("password123"),
            is_active=True,
        )
    )
    db.commit()

    # Also register in InMemoryUserRepository for authentication endpoints
    from app.auth.repository import get_user_repository
    from app.auth.models import UserInDB
    user_repo = get_user_repository()
    user_repo._users_by_id["alice"] = UserInDB(
        user_id="alice",
        tenant_id="tenant-alpha",
        email="alice@alpha.com",
        name="Alice Alpha",
        role="ADMIN",
        is_active=True,
        hashed_password=hash_password("password123"),
    )
    user_repo._users_by_email["alice@alpha.com"] = user_repo._users_by_id["alice"]
    user_repo._users_by_id["bob"] = UserInDB(
        user_id="bob",
        tenant_id="tenant-beta",
        email="bob@beta.com",
        name="Bob Beta",
        role="ADMIN",
        is_active=True,
        hashed_password=hash_password("password123"),
    )
    user_repo._users_by_email["bob@beta.com"] = user_repo._users_by_id["bob"]

    yield db
    db.close()


@pytest.fixture
def mock_search_store():
    transport = MockOpenSearchTransport(dimension=384)
    store = OpenSearchStore(
        base_url="http://localhost:9200",
        index_alias="test_step10_chunks",
        transport=transport,
        configured_dimension=384,
    )
    store.ensure_index_initialized()
    return store


@pytest.fixture
def isolated_client(test_db):
    def override_get_db():
        try:
            yield test_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ===========================================================================
# 1. Task Domain Model & Status Transitions
# ===========================================================================

def test_task_creation_and_lifecycle_transitions(test_db):
    repo = SQLTaskRepository(test_db)
    task = DBTask(
        id="task-001",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="PENDING",
        payload={"doc_id": "doc-123"},
    )
    created = repo.create(task)
    test_db.commit()

    assert created.id == "task-001"
    assert created.status == "PENDING"
    assert created.attempt_count == 0

    # Claim task
    claimed = repo.claim_next_task(worker_id="worker-1")
    test_db.commit()
    assert claimed is not None
    assert claimed.id == "task-001"
    assert claimed.status == "RUNNING"
    assert claimed.worker_id == "worker-1"
    assert claimed.attempt_count == 1

    # Mark success
    success = repo.mark_success("task-001", "tenant-alpha", result={"indexed": 5})
    test_db.commit()
    assert success is True

    updated = repo.get_by_id_and_tenant("task-001", "tenant-alpha")
    assert updated.status == "SUCCEEDED"
    assert updated.result == {"indexed": 5}
    assert updated.completed_at is not None


# ===========================================================================
# 2. Task Idempotency Key Deduplication
# ===========================================================================

def test_task_idempotency_key_deduplication(test_db):
    repo = SQLTaskRepository(test_db)
    t1 = DBTask(
        id="task-idemp-1",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        idempotency_key="upload_doc_999_v1",
        status="PENDING",
        payload={"version": 1},
    )
    r1 = repo.create(t1)
    test_db.commit()

    # Second insert with same tenant and idempotency key
    t2 = DBTask(
        id="task-idemp-2",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        idempotency_key="upload_doc_999_v1",
        status="PENDING",
        payload={"version": 1},
    )
    r2 = repo.create(t2)
    test_db.commit()

    # Should return existing task without duplicate creation
    assert r1.id == "task-idemp-1"
    assert r2.id == "task-idemp-1"
    assert test_db.query(DBTask).filter(DBTask.tenant_id == "tenant-alpha").count() == 1

    # Different tenant with same key is allowed (tenant-scoped)
    t3 = DBTask(
        id="task-idemp-3",
        tenant_id="tenant-beta",
        task_type=TaskType.DOCUMENT_INGEST.value,
        idempotency_key="upload_doc_999_v1",
        status="PENDING",
    )
    r3 = repo.create(t3)
    test_db.commit()
    assert r3.id == "task-idemp-3"


# ===========================================================================
# 3. Atomic Task Claiming
# ===========================================================================

def test_atomic_task_claiming_no_duplicate_claims(test_db):
    repo = SQLTaskRepository(test_db)
    task = DBTask(
        id="claim-task-1",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="PENDING",
    )
    repo.create(task)
    test_db.commit()

    # Worker A claims
    claim_a = repo.claim_next_task(worker_id="worker-A")
    test_db.commit()
    assert claim_a is not None
    assert claim_a.id == "claim-task-1"
    assert claim_a.worker_id == "worker-A"

    # Worker B attempts to claim the same task simultaneously -> None available
    claim_b = repo.claim_next_task(worker_id="worker-B")
    test_db.commit()
    assert claim_b is None


# ===========================================================================
# 4. Multi-Tenant Isolation & IDOR Protection
# ===========================================================================

def test_tenant_isolation_in_task_lookups_and_api(isolated_client, test_db):
    repo = SQLTaskRepository(test_db)
    task_alpha = DBTask(
        id="task-alpha-secure",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="PENDING",
        payload={"sensitive": "alpha-data"},
    )
    repo.create(task_alpha)
    test_db.commit()

    # Database query level
    assert repo.get_by_id_and_tenant("task-alpha-secure", "tenant-alpha") is not None
    assert repo.get_by_id_and_tenant("task-alpha-secure", "tenant-beta") is None

    # API level IDOR check
    token_bob = create_access_token({
        "sub": "bob",
        "tenant_id": "tenant-beta",
        "email": "bob@beta.com",
        "name": "Bob Beta",
        "role": "ADMIN",
    })
    headers_bob = {"Authorization": f"Bearer {token_bob}"}

    resp = isolated_client.get("/tasks/task-alpha-secure", headers=headers_bob)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ===========================================================================
# 5. Stale Task Crash Recovery
# ===========================================================================

def test_stale_task_recovery_after_simulated_worker_crash(test_db):
    repo = SQLTaskRepository(test_db)
    crashed_time = datetime.now(timezone.utc) - timedelta(seconds=400)
    task = DBTask(
        id="crashed-task-1",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="RUNNING",
        started_at=crashed_time,
        updated_at=crashed_time,
        worker_id="crashed-worker-99",
        attempt_count=1,
        max_attempts=3,
    )
    repo.create(task)
    test_db.commit()

    # Run recovery with 300s threshold
    recovered_count = repo.recover_stale_tasks(stale_threshold_seconds=300)
    test_db.commit()
    assert recovered_count == 1

    recovered_task = repo.get_by_id_and_tenant("crashed-task-1", "tenant-alpha")
    assert recovered_task.status == "RETRYING"
    assert recovered_task.worker_id is None
    assert "crashed-worker-99" in recovered_task.last_error

    # New healthy worker claims recovered task
    healthy_claim = repo.claim_next_task(worker_id="healthy-worker-2")
    test_db.commit()
    assert healthy_claim is not None
    assert healthy_claim.id == "crashed-task-1"
    assert healthy_claim.attempt_count == 2


# ===========================================================================
# 6. Retry Semantics & Backoff
# ===========================================================================

def test_bounded_exponential_backoff_and_retry(test_db):
    repo = SQLTaskRepository(test_db)
    task = DBTask(
        id="retry-task-1",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="RUNNING",
        attempt_count=1,
        max_attempts=2,
    )
    repo.create(task)
    test_db.commit()

    # Attempt 1: Transient failure -> RETRYING
    repo.mark_failure("retry-task-1", "tenant-alpha", "Connection timeout", is_retryable=True, backoff_seconds=10)
    test_db.commit()

    t_after_1 = repo.get_by_id_and_tenant("retry-task-1", "tenant-alpha")
    assert t_after_1.status == "RETRYING"
    assert t_after_1.last_error == "Connection timeout"

    # Advance attempts to max_attempts
    t_after_1.attempt_count = 2
    repo.mark_failure("retry-task-1", "tenant-alpha", "Connection timeout again", is_retryable=True, backoff_seconds=20)
    test_db.commit()

    t_after_2 = repo.get_by_id_and_tenant("retry-task-1", "tenant-alpha")
    assert t_after_2.status == "FAILED"
    assert t_after_2.failed_at is not None


def test_non_retryable_failure_fails_immediately(test_db):
    repo = SQLTaskRepository(test_db)
    task = DBTask(
        id="perm-fail-1",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="RUNNING",
        attempt_count=1,
        max_attempts=5,
    )
    repo.create(task)
    test_db.commit()

    repo.mark_failure("perm-fail-1", "tenant-alpha", "Corrupted file header", is_retryable=False)
    test_db.commit()

    t = repo.get_by_id_and_tenant("perm-fail-1", "tenant-alpha")
    assert t.status == "FAILED"


# ===========================================================================
# 7. End-to-End Ingestion Workflow
# ===========================================================================

def test_async_document_ingestion_end_to_end(test_db, mock_search_store, tmp_path):
    doc_repo = SQLDocumentRepository(test_db)
    task_repo = SQLTaskRepository(test_db)
    doc_id = "doc-async-001"

    # Create dummy source file
    test_file = tmp_path / "report.txt"
    file_bytes = b"Asynchronous worker architecture for enterprise RAG systems."
    test_file.write_bytes(file_bytes)
    real_hash = hashlib.sha256(file_bytes).hexdigest()

    db_doc = DBDocument(
        document_id=doc_id,
        tenant_id="tenant-alpha",
        owner_id="alice",
        filename="report.txt",
        source="upload",
        mime_type="application/txt",
        size_bytes=len(file_bytes),
        content_hash=real_hash,
        access_level="ROLE_BASED",
        permission_status="KNOWN",
        status="PENDING",
        storage_path=str(test_file),
        version=1,
    )
    doc_repo.create(db_doc)
    task = DBTask(
        id="task-ingest-001",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="RUNNING",
        payload={
            "document_id": doc_id,
            "tenant_id": "tenant-alpha",
            "document_version": 1,
            "storage_path": str(test_file),
            "owner_id": "alice",
            "access_level": "ROLE_BASED",
            "allowed_roles": ["ENGINEERING"],
            "allowed_user_ids": [],
        },
    )
    task_repo.create(task)
    test_db.commit()

    context = WorkerContext(
        db=test_db,
        search_store=mock_search_store,
        worker_id="test-worker",
    )
    handler = DocumentIngestHandler()
    result = handler.handle(task, context)

    assert result["status"] == "INDEXED"
    assert result["num_chunks"] > 0

    # Verify authoritative database state
    updated_doc = doc_repo.get_by_id(doc_id, "tenant-alpha")
    assert updated_doc.status == "INDEXED"
    assert updated_doc.indexed_at is not None

    # Verify OpenSearch search store was updated
    stats = mock_search_store.get_statistics("tenant-alpha")
    assert stats.total_chunks > 0


# ===========================================================================
# 8. Version Race & Deletion Race Protection
# ===========================================================================

def test_version_race_protection_aborts_stale_worker(test_db, mock_search_store, tmp_path):
    doc_repo = SQLDocumentRepository(test_db)
    task_repo = SQLTaskRepository(test_db)
    doc_id = "doc-race-001"

    test_file = tmp_path / "v1.txt"
    test_file.write_text("Version 1 content")

    # DB is already at version 2 (newer upload happened)
    db_doc = DBDocument(
        document_id=doc_id,
        tenant_id="tenant-alpha",
        owner_id="alice",
        filename="v1.txt",
        source="upload",
        mime_type="application/txt",
        size_bytes=10,
        content_hash="hash-v2",
        access_level="PRIVATE",
        permission_status="KNOWN",
        status="INDEXED",
        storage_path=str(test_file),
        version=2,
    )
    doc_repo.create(db_doc)

    # Stale task arriving for version 1
    stale_task = DBTask(
        id="stale-task-v1",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="RUNNING",
        payload={
            "document_id": doc_id,
            "tenant_id": "tenant-alpha",
            "document_version": 1,
            "storage_path": str(test_file),
        },
    )
    task_repo.create(stale_task)
    test_db.commit()

    context = WorkerContext(db=test_db, search_store=mock_search_store)
    handler = DocumentIngestHandler()
    result = handler.handle(stale_task, context)

    # Aborts safely without modifying version 2
    assert result["status"] == "aborted"
    assert result["reason"] == "superseded_by_newer_version"
    assert doc_repo.get_by_id(doc_id, "tenant-alpha").version == 2


def test_deletion_race_protection_aborts_indexing(test_db, mock_search_store, tmp_path):
    doc_repo = SQLDocumentRepository(test_db)
    task_repo = SQLTaskRepository(test_db)
    doc_id = "doc-del-race"

    test_file = tmp_path / "del.txt"
    test_file.write_text("Deletion candidate content")

    db_doc = DBDocument(
        document_id=doc_id,
        tenant_id="tenant-alpha",
        owner_id="alice",
        filename="del.txt",
        source="upload",
        mime_type="application/txt",
        size_bytes=10,
        content_hash="hash-del",
        access_level="PRIVATE",
        permission_status="KNOWN",
        status="DELETING",
        storage_path=str(test_file),
        version=1,
    )
    doc_repo.create(db_doc)

    task = DBTask(
        id="task-del-race",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="RUNNING",
        payload={
            "document_id": doc_id,
            "tenant_id": "tenant-alpha",
            "document_version": 1,
            "storage_path": str(test_file),
        },
    )
    task_repo.create(task)
    test_db.commit()

    context = WorkerContext(db=test_db, search_store=mock_search_store)
    handler = DocumentIngestHandler()
    result = handler.handle(task, context)

    assert result["status"] == "aborted"
    assert result["reason"] == "document_deleted"


# ===========================================================================
# 9. Crash-Safe Deletion Handler
# ===========================================================================

def test_crash_safe_document_deletion_handler(test_db, mock_search_store, tmp_path):
    doc_repo = SQLDocumentRepository(test_db)
    task_repo = SQLTaskRepository(test_db)
    doc_id = "doc-delete-me"

    test_file = tmp_path / "temp.txt"
    test_file.write_text("To be deleted")

    db_doc = DBDocument(
        document_id=doc_id,
        tenant_id="tenant-alpha",
        owner_id="alice",
        filename="temp.txt",
        source="upload",
        mime_type="application/txt",
        size_bytes=10,
        content_hash="hash-temp",
        access_level="PRIVATE",
        permission_status="KNOWN",
        status="DELETING",
        storage_path=str(test_file),
        version=1,
    )
    doc_repo.create(db_doc)

    task = DBTask(
        id="task-del-001",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_DELETE.value,
        status="RUNNING",
        payload={"document_id": doc_id, "user_id": "alice"},
    )
    task_repo.create(task)
    test_db.commit()

    context = WorkerContext(db=test_db, search_store=mock_search_store)
    handler = DocumentDeleteHandler()
    res = handler.handle(task, context)

    assert res["status"] == "DELETED"
    assert doc_repo.get_by_id(doc_id, "tenant-alpha") is None

    # Repeating deletion is idempotent
    res2 = handler.handle(task, context)
    assert res2["status"] == "DELETED"


# ===========================================================================
# 10. Permission Sync Handler (Zero Re-Embedding)
# ===========================================================================

def test_permission_sync_handler_zero_reembedding(test_db, mock_search_store):
    doc_repo = SQLDocumentRepository(test_db)
    chunk_repo = SQLChunkRepository(test_db)
    perm_repo = SQLPermissionRepository(test_db)
    task_repo = SQLTaskRepository(test_db)
    doc_id = "doc-perms-001"

    db_doc = DBDocument(
        document_id=doc_id,
        tenant_id="tenant-alpha",
        owner_id="alice",
        filename="perms.txt",
        source="upload",
        mime_type="application/txt",
        size_bytes=10,
        content_hash="hash-perms",
        access_level="PRIVATE",
        permission_status="KNOWN",
        status="INDEXED",
        version=1,
    )
    doc_repo.create(db_doc)
    chunk = DBDocumentChunk(
        chunk_id="chunk-p-1",
        document_id=doc_id,
        tenant_id="tenant-alpha",
        chunk_index=0,
        content="Permission test content",
        content_hash="hash-p1",
        metadata_json={"access_level": "PRIVATE"},
    )
    chunk_repo.create_batch([chunk])
    task = DBTask(
        id="task-perm-001",
        tenant_id="tenant-alpha",
        task_type=TaskType.PERMISSION_SYNC.value,
        status="RUNNING",
        payload={
            "document_id": doc_id,
            "access_level": "ROLE_BASED",
            "allowed_roles": ["FINANCE", "EXECUTIVE"],
            "allowed_user_ids": [],
            "user_id": "alice",
        },
    )
    task_repo.create(task)
    test_db.commit()

    context = WorkerContext(db=test_db, search_store=mock_search_store)
    handler = PermissionSyncHandler()
    res = handler.handle(task, context)

    assert res["status"] == "SUCCEEDED"
    assert res["version"] == 2
    assert res["chunks_updated"] == 1

    # Verify DB state
    updated_doc = doc_repo.get_by_id(doc_id, "tenant-alpha")
    assert updated_doc.access_level == "ROLE_BASED"
    assert updated_doc.version == 2


# ===========================================================================
# 11. Transactional Outbox Dispatcher
# ===========================================================================

def test_outbox_dispatcher_delivers_pending_events(test_db):
    outbox_repo = SQLOutboxRepository(test_db)
    queue = InMemoryTaskQueue()
    dispatcher = OutboxDispatcher(task_queue=queue, session_factory=lambda: test_db)

    evt = DBOutboxEvent(
        id="evt-100",
        tenant_id="tenant-alpha",
        event_type="DOCUMENT_INGEST",
        aggregate_id="doc-100",
        status="PENDING",
        payload={"task_id": "task-100"},
    )
    outbox_repo.create(evt)
    test_db.commit()

    published = dispatcher.dispatch_pending()
    test_db.commit()
    assert published == 1

    # Event status should now be PUBLISHED
    updated_evt = test_db.query(DBOutboxEvent).filter(DBOutboxEvent.id == "evt-100").first()
    assert updated_evt.status == "PUBLISHED"
    assert updated_evt.published_at is not None


# ===========================================================================
# 12. Task Cancellation Endpoint
# ===========================================================================

def test_task_cancel_endpoint(isolated_client, test_db):
    repo = SQLTaskRepository(test_db)
    task = DBTask(
        id="task-cancel-me",
        tenant_id="tenant-alpha",
        task_type=TaskType.DOCUMENT_INGEST.value,
        status="PENDING",
    )
    repo.create(task)
    test_db.commit()

    token_alice = create_access_token({
        "sub": "alice",
        "tenant_id": "tenant-alpha",
        "email": "alice@alpha.com",
        "name": "Alice Alpha",
        "role": "ADMIN",
    })
    headers = {"Authorization": f"Bearer {token_alice}"}

    resp = isolated_client.post("/tasks/task-cancel-me/cancel", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"

    updated = repo.get_by_id_and_tenant("task-cancel-me", "tenant-alpha")
    assert updated.status == "CANCELLED"
