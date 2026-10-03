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

    from app.storage.search.dev_adapters import DevelopmentHybridSearchStore

    mock_rag = MagicMock()
    mock_rag.vector_db.vectordb.add_documents.return_value = ["chunk_1"]
    mock_rag.vector_db.vectordb.delete.return_value = None
    mock_rag.retrieval_pipeline.rebuild_bm25.return_value = None
    app.state.rag_service = mock_rag
    app.state.search_store = DevelopmentHybridSearchStore(vector_db=mock_rag.vector_db)

    client = TestClient(app)
    yield client, TestingSession
    app.dependency_overrides.clear()
    if hasattr(app.state, "rag_service"):
        delattr(app.state, "rag_service")
    if hasattr(app.state, "search_store"):
        delattr(app.state, "search_store")


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


def test_batch_embedding_in_ingestion_handler(lifecycle_client):
    from app.tasks.handlers.ingestion import DocumentIngestHandler
    from app.tasks.handlers.base import WorkerContext
    from app.storage.models.task import Task
    from app.storage.repositories.document_repository import SQLDocumentRepository
    from datetime import datetime, timezone

    _, SessionMaker = lifecycle_client
    db = SessionMaker()

    file_bytes = b"Chunk one content. Chunk two content. Chunk three content."
    import hashlib
    actual_hash = hashlib.sha256(file_bytes).hexdigest()

    doc_repo = SQLDocumentRepository(db)
    doc = doc_repo.create(DBDocument(
        document_id="doc_batch_embed",
        tenant_id="company_a",
        filename="batch.txt",
        storage_path="data/uploads/batch.txt",
        content_hash=actual_hash,
        status="PENDING",
        owner_id="admin_a",
        access_level="PRIVATE",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    ))
    db.commit()

    # Create dummy local file
    os.makedirs("data/uploads", exist_ok=True)
    with open("data/uploads/batch.txt", "wb") as f:
        f.write(file_bytes)

    mock_embedder = MagicMock()
    mock_embedder.embed_documents.side_effect = lambda texts: [[0.01 * (i + 1)] * 384 for i in range(len(texts))]

    mock_rag = MagicMock()
    mock_rag.embeddings = mock_embedder

    mock_search_store = MagicMock()

    context = WorkerContext(
        db=db,
        search_store=mock_search_store,
        blob_storage=get_document_storage(),
        secret_provider=MagicMock(),
        rag_service=mock_rag,
        worker_id="worker-1",
    )

    task = Task(
        id="task_batch_test",
        tenant_id="company_a",
        task_type="DOCUMENT_INGEST",
        status="RUNNING",
        payload={
            "document_id": "doc_batch_embed",
            "document_version": 1,
            "storage_path": "data/uploads/batch.txt",
            "owner_id": "admin_a",
            "access_level": "PRIVATE",
        },
        attempt_count=1,
        max_attempts=3,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    handler = DocumentIngestHandler()
    res = handler.handle(task, context)
    assert res["status"] == "INDEXED"

    # Verify embed_documents was called EXACTLY ONCE with list of all chunk texts
    assert mock_embedder.embed_documents.call_count == 1
    assert mock_embedder.embed_query.call_count == 0

    # Verify search_store.index_chunks was passed precomputed 384-dim vectors
    assert mock_search_store.index_chunks.call_count == 1
    payloads = mock_search_store.index_chunks.call_args[0][0]
    assert len(payloads) >= 1
    for p in payloads:
        assert isinstance(p.embedding, list)
        assert len(p.embedding) == 384

    # Cleanup test file
    if os.path.exists("data/uploads/batch.txt"):
        os.remove("data/uploads/batch.txt")
    db.close()


def test_stale_recovery_with_updated_at_heartbeat(lifecycle_client):
    from app.storage.repositories.task_repository import SQLTaskRepository
    from app.storage.models.task import Task
    from datetime import datetime, timezone, timedelta

    _, SessionMaker = lifecycle_client
    db = SessionMaker()
    repo = SQLTaskRepository(db)

    now = datetime.now(timezone.utc)
    old_time = now - timedelta(seconds=600)  # Started 10 minutes ago

    # Active task: started 10 minutes ago, but updated 1 minute ago (active heartbeat/progress)
    active_task = Task(
        id="task_active_heartbeat",
        tenant_id="company_a",
        task_type="DOCUMENT_INGEST",
        status="RUNNING",
        worker_id="worker-active",
        attempt_count=1,
        max_attempts=3,
        started_at=old_time,
        updated_at=now - timedelta(seconds=60),  # Active within threshold
        created_at=old_time,
    )

    # Dead task: started 10 minutes ago and updated 10 minutes ago (crashed worker)
    crashed_task = Task(
        id="task_crashed_worker",
        tenant_id="company_a",
        task_type="DOCUMENT_INGEST",
        status="RUNNING",
        worker_id="worker-crashed",
        attempt_count=1,
        max_attempts=3,
        started_at=old_time,
        updated_at=old_time,  # Inactive
        created_at=old_time,
    )

    repo.create(active_task)
    repo.create(crashed_task)
    db.commit()

    # Run stale recovery with 300s threshold
    recovered_count = repo.recover_stale_tasks(stale_threshold_seconds=300)
    assert recovered_count == 1  # Only the truly crashed task is recovered

    db.refresh(active_task)
    db.refresh(crashed_task)

    assert active_task.status == "RUNNING"
    assert active_task.worker_id == "worker-active"

    assert crashed_task.status == "RETRYING"
    assert crashed_task.worker_id is None
    db.close()

