"""
test_seed_startup.py — Tests for database seeding & AUTO_SEED_DATA startup hook.
"""

import os
import sys
import pytest
from unittest.mock import patch

# Ensure backend directory is in path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from scripts.seed_dev import seed_database
from app.storage.database import SessionLocal, init_db
from app.storage.models import User as DBUser, Tenant


def test_seed_database_execution_and_idempotency():
    """Verify seed_database populates users and tenants idempotently."""
    # First execution
    seed_database()

    db = SessionLocal()
    try:
        admin_user = db.query(DBUser).filter(DBUser.user_id == "admin_a").first()
        assert admin_user is not None
        assert admin_user.email == "admin@companya.com"
        assert admin_user.role == "ADMIN"

        tenant_a = db.query(Tenant).filter(Tenant.tenant_id == "company_a").first()
        assert tenant_a is not None
    finally:
        db.close()

    # Second execution (must succeed without primary key conflict)
    seed_database()


def test_seed_database_custom_env_passwords(monkeypatch):
    """Verify seed_database uses custom password environment variables."""
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "custom_admin_pass_99")
    try:
        seed_database()

        db = SessionLocal()
        try:
            from app.auth.password import verify_password
            admin_user = db.query(DBUser).filter(DBUser.user_id == "admin_a").first()
            assert admin_user is not None
            assert verify_password("custom_admin_pass_99", admin_user.password_hash)
        finally:
            db.close()
    finally:
        monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
        seed_database()



import asyncio


def test_lifespan_startup_seeding_hook(monkeypatch):
    """Verify FastAPI lifespan triggers seeding when AUTO_SEED_DATA=true."""
    from app import config
    from app.main import lifespan, app

    monkeypatch.setattr(config, "AUTO_SEED_DATA", True)

    async def _test():
        async with lifespan(app):
            # Verify app is alive and seeded
            db = SessionLocal()
            try:
                admin_user = db.query(DBUser).filter(DBUser.user_id == "admin_a").first()
                assert admin_user is not None
            finally:
                db.close()

    asyncio.run(_test())


def test_lifespan_startup_seeding_error_resilience(monkeypatch):
    """Verify application startup continues cleanly even if seeding fails."""
    from app import config
    from app.main import lifespan, app

    monkeypatch.setattr(config, "AUTO_SEED_DATA", True)

    async def _test():
        with patch("scripts.seed_dev.seed_database", side_effect=RuntimeError("DB seed error simulate")):
            # Lifespan should catch error and continue startup
            async with lifespan(app):
                pass  # Should reach here without raising exception

    asyncio.run(_test())


def test_production_repository_selection_uses_sql_user_repository(monkeypatch):
    """Verify that get_user_repository returns SQLUserRepository by default in production."""
    from app import config
    from app.auth.repository import get_user_repository, SQLUserRepository, set_user_repository

    set_user_repository(None)  # Reset any test override
    monkeypatch.setattr(config, "USER_REPOSITORY_TYPE", "sql")

    repo = get_user_repository()
    assert isinstance(repo, SQLUserRepository)


def test_sql_user_repository_authentication(monkeypatch):
    """Verify AuthService authenticates seeded database users using custom environment passwords."""
    from app import config
    from app.auth.service import AuthService
    from app.auth.repository import SQLUserRepository, set_user_repository

    set_user_repository(None)
    monkeypatch.setattr(config, "USER_REPOSITORY_TYPE", "sql")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "custom_sql_admin_pass_123")

    try:
        # Seed database with custom admin password
        seed_database()

        service = AuthService(repository=SQLUserRepository())

        # 1. Seeded DB User authenticates successfully
        user, err = service.authenticate_user("admin@companya.com", "custom_sql_admin_pass_123")
        assert err is None
        assert user is not None
        assert user.user_id == "admin_a"
        assert user.role == "ADMIN"

        # 2. Issue token for authenticated user
        token = service.issue_token(user)
        assert token.access_token is not None
        assert token.token_type == "bearer"

        # 3. Wrong password returns 401 / Invalid
        user_wrong, err_wrong = service.authenticate_user("admin@companya.com", "wrong_password_99")
        assert user_wrong is None
        assert "Invalid" in err_wrong

        # 4. Inactive user returns 401 / inactive error
        user_inact, err_inact = service.authenticate_user("inactive@companya.com", "inactive123")
        assert user_inact is None
        assert "inactive" in err_inact.lower()

        # 5. Unknown user returns 401 / Invalid error
        user_unk, err_unk = service.authenticate_user("unknown@companya.com", "somepassword")
        assert user_unk is None
        assert "Invalid" in err_unk
    finally:
        # Restore default database seed users for subsequent test files
        monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
        seed_database()


def test_embedding_model_singleton_caching_and_dimensions():
    """Verify load_embedding_model caches model instances and produces 384d embeddings."""
    from app.core.embeddings import load_embedding_model, get_embedding_dimension
    from app import config

    model_name = config.EMBEDDING_MODEL_NAME

    model_1 = load_embedding_model(model_name)
    model_2 = load_embedding_model(model_name)

    # 1. Verify exact same instance is returned (singleton reference equality)
    assert model_1 is model_2

    # 2. Verify embedding dimension is 384
    dim = get_embedding_dimension(model_1)
    assert dim == 384

    # 3. Verify sample embedding generation works and produces a 384-element vector
    vec = model_1.embed_query("test semantic sentence for embedding verification")
    assert isinstance(vec, list)
    assert len(vec) == 384


def test_embedding_runtime_thread_constraints():
    """Verify PyTorch and CPU threading environment variables are configured for single-thread execution."""
    import os
    import torch
    from app.core.embeddings import load_embedding_model
    from app import config

    assert os.environ.get("OMP_NUM_THREADS") == "1"
    assert os.environ.get("MKL_NUM_THREADS") == "1"
    assert os.environ.get("OPENBLAS_NUM_THREADS", "1") == "1"
    assert os.environ.get("TOKENIZERS_PARALLELISM") == "false"
    assert torch.get_num_threads() == 1

    model = load_embedding_model(config.EMBEDDING_MODEL_NAME)
    assert model is not None


def test_single_vector_indexing_invocation_during_ingestion():
    """Verify DocumentIngestHandler calls search store index_chunks exactly once."""
    from unittest.mock import MagicMock, patch
    from app.tasks.handlers.ingestion import DocumentIngestHandler
    from app.tasks.handlers.base import WorkerContext
    from app.storage.models.task import Task
    from app.storage.database import SessionLocal
    from app.storage.models import Document as DBDocument, Tenant

    # Create dummy database session
    db = SessionLocal()
    try:
        # Create test tenant & document
        tenant = db.query(Tenant).filter(Tenant.tenant_id == "company_a").first()
        if not tenant:
            db.add(Tenant(tenant_id="company_a", name="Company A"))
            db.commit()

        doc_id = "test_single_index_doc_1"
        db_doc = db.query(DBDocument).filter(DBDocument.document_id == doc_id).first()
        if not db_doc:
            db_doc = DBDocument(
                document_id=doc_id,
                tenant_id="company_a",
                owner_id="admin_a",
                filename="test.txt",
                mime_type="text/plain",
                size_bytes=100,
                content_hash="dummyhash123",
                access_level="PRIVATE",
                status="PENDING",
                version=1,
            )
            db.add(db_doc)
            db.commit()
        else:
            db_doc.status = "PENDING"
            db_doc.version = 1
            db.commit()

        # Mock search store & blob storage
        mock_search_store = MagicMock()
        mock_blob_storage = MagicMock()
        mock_blob_storage.download_verified.return_value = b"Hello world test content for ingestion"

        task = Task(
            id="test_task_101",
            tenant_id="company_a",
            task_type="DOCUMENT_INGEST",
            payload={
                "document_id": doc_id,
                "document_version": 1,
                "storage_path": "dummy_path.txt",
                "owner_id": "admin_a",
                "access_level": "PRIVATE",
            },
        )

        context = WorkerContext(
            db=db,
            search_store=mock_search_store,
            blob_storage=mock_blob_storage,
            secret_provider=MagicMock(),
            rag_service=MagicMock(),
            worker_id="test_worker_1",
        )

        handler = DocumentIngestHandler()
        with patch("app.tasks.handlers.ingestion.ingest_document") as mock_ingest:
            from langchain_core.documents import Document as LCDoc
            mock_ingest.return_value = [LCDoc(page_content="Hello world test content", metadata={})]

            result = handler.handle(task, context)

            # Assert search_store.index_chunks was called EXACTLY ONCE
            assert mock_search_store.index_chunks.call_count == 1
            assert result["status"] == "INDEXED"
    finally:
        db.close()





