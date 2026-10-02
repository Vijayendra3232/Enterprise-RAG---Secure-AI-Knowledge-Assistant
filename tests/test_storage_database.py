"""
test_storage_database.py — Unit tests for SQLAlchemy database models and repositories.
Verifies CRUD operations, foreign key cascades, constraints, and audit logging.
"""

import os
import sys
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure backend directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.storage.database import Base
from app.storage.models import (
    Tenant,
    User as DBUser,
    Document as DBDocument,
    DocumentPermission as DBDocumentPermission,
    DocumentChunk as DBDocumentChunk,
    AuditEvent,
)
from app.storage.repositories import (
    SQLTenantRepository,
    SQLUserRepository,
    SQLDocumentRepository,
    SQLPermissionRepository,
    SQLChunkRepository,
    SQLAuditRepository,
    DuplicateEntityException,
    EntityNotFoundException,
    ConcurrencyConflictException,
)
from app.auth.models import UserInDB
from app.auth.password import hash_password, verify_password


@pytest.fixture
def db_session():
    """Create an isolated, in-memory SQLite database session for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_tenant_repository(db_session):
    repo = SQLTenantRepository(db_session)
    
    # Create
    tenant = Tenant(tenant_id="tenant_x", name="Tenant X Corp", is_active=True)
    created = repo.create(tenant)
    assert created.tenant_id == "tenant_x"

    # Duplicate create raises exception
    with pytest.raises(DuplicateEntityException):
        repo.create(Tenant(tenant_id="tenant_x", name="Tenant X Duplicate"))

    # Get by ID
    fetched = repo.get_by_id("tenant_x")
    assert fetched is not None
    assert fetched.name == "Tenant X Corp"

    # List active
    active_tenants = repo.list_active()
    assert len(active_tenants) == 1

    # Get or create
    existing = repo.get_or_create("tenant_x", "Tenant X")
    assert existing.tenant_id == "tenant_x"
    new_t = repo.get_or_create("tenant_y", "Tenant Y")
    assert new_t.tenant_id == "tenant_y"


def test_user_repository(db_session):
    tenant_repo = SQLTenantRepository(db_session)
    tenant_repo.create(Tenant(tenant_id="tenant_1", name="Tenant 1"))
    tenant_repo.create(Tenant(tenant_id="tenant_2", name="Tenant 2"))

    user_repo = SQLUserRepository(db_session)

    # Create user
    user1 = UserInDB(
        user_id="usr_001",
        tenant_id="tenant_1",
        email="alice@tenant1.com",
        name="Alice Smith",
        role="ADMIN",
        is_active=True,
        groups=["admins"],
        hashed_password=hash_password("alice_pass"),
    )
    created = user_repo.create(user1)
    assert created.user_id == "usr_001"

    # Duplicate user_id raises DuplicateEntityException
    with pytest.raises(DuplicateEntityException):
        user_repo.create(user1)

    # Duplicate email in same tenant raises DuplicateEntityException
    with pytest.raises(DuplicateEntityException):
        user_repo.create(
            UserInDB(
                user_id="usr_002",
                tenant_id="tenant_1",
                email="alice@tenant1.com",
                name="Alice Clone",
                role="USER",
                is_active=True,
                hashed_password=hash_password("pass"),
            )
        )

    # Same email in different tenant is permitted by composite unique constraint
    user2 = UserInDB(
        user_id="usr_003",
        tenant_id="tenant_2",
        email="alice@tenant1.com",
        name="Alice in Tenant 2",
        role="VIEWER",
        is_active=True,
        hashed_password=hash_password("pass2"),
    )
    created2 = user_repo.create(user2)
    assert created2.tenant_id == "tenant_2"

    # Retrieval
    fetched_user = user_repo.get_by_email("alice@tenant1.com", tenant_id="tenant_1")
    assert fetched_user is not None
    assert fetched_user.user_id == "usr_001"
    assert verify_password("alice_pass", fetched_user.hashed_password)

    # List by tenant
    tenant1_users = user_repo.list_by_tenant("tenant_1")
    assert len(tenant1_users) == 1
    assert tenant1_users[0].user_id == "usr_001"


def test_document_repository(db_session):
    tenant_repo = SQLTenantRepository(db_session)
    tenant_repo.create(Tenant(tenant_id="tenant_1", name="Tenant 1"))

    doc_repo = SQLDocumentRepository(db_session)

    # Create document
    doc = DBDocument(
        document_id="doc_abc123",
        tenant_id="tenant_1",
        owner_id="usr_001",
        filename="report.pdf",
        source="upload",
        source_document_id=None,
        mime_type="application/pdf",
        size_bytes=1024,
        content_hash="hash_12345",
        access_level="PRIVATE",
        permission_status="KNOWN",
        status="PENDING",
    )
    created_doc = doc_repo.create(doc)
    assert created_doc.document_id == "doc_abc123"
    assert created_doc.version == 1

    # State transitions: PENDING -> PROCESSING -> INDEXED
    doc_repo.update_status("doc_abc123", status="PROCESSING")
    fetched = doc_repo.get_by_id("doc_abc123")
    assert fetched.status == "PROCESSING"
    assert fetched.version == 2

    now_ts = datetime.now(timezone.utc)
    doc_repo.update_status("doc_abc123", status="INDEXED", indexed_at=now_ts)
    fetched = doc_repo.get_by_id("doc_abc123")
    assert fetched.status == "INDEXED"
    assert fetched.indexed_at is not None

    # Optimistic concurrency update
    fetched.filename = "report_v2.pdf"
    updated = doc_repo.update_document(fetched, expected_version=3)
    assert updated.version == 4
    assert updated.filename == "report_v2.pdf"

    # Mismatched expected version raises ConcurrencyConflictException
    with pytest.raises(ConcurrencyConflictException):
        doc_repo.update_document(fetched, expected_version=1)

    # Get by content hash
    by_hash = doc_repo.get_by_content_hash("tenant_1", "hash_12345")
    assert by_hash is not None
    assert by_hash.document_id == "doc_abc123"

    # Delete
    deleted = doc_repo.delete("doc_abc123", tenant_id="tenant_1")
    assert deleted is True
    assert doc_repo.get_by_id("doc_abc123") is None


def test_permission_and_chunk_repositories(db_session):
    tenant_repo = SQLTenantRepository(db_session)
    tenant_repo.create(Tenant(tenant_id="tenant_1", name="Tenant 1"))

    doc_repo = SQLDocumentRepository(db_session)
    doc = DBDocument(
        document_id="doc_xyz",
        tenant_id="tenant_1",
        filename="handbook.txt",
        source="upload",
        content_hash="hash_xyz",
        access_level="ROLE_BASED",
        status="INDEXED",
    )
    doc_repo.create(doc)

    perm_repo = SQLPermissionRepository(db_session)
    chunk_repo = SQLChunkRepository(db_session)

    # Set permissions
    perms = perm_repo.set_permissions(
        document_id="doc_xyz",
        tenant_id="tenant_1",
        roles=["ENGINEERING", "PRODUCT"],
        user_ids=["user_special"],
    )
    assert len(perms) == 3

    fetched_perms = perm_repo.get_for_document("doc_xyz", tenant_id="tenant_1")
    assert len(fetched_perms) == 3
    roles = {p.role for p in fetched_perms if p.role}
    assert roles == {"ENGINEERING", "PRODUCT"}

    # Create chunks
    chunks = [
        DBDocumentChunk(
            chunk_id="doc_xyz_0",
            document_id="doc_xyz",
            tenant_id="tenant_1",
            chunk_index=0,
            content_hash="c_hash_0",
            content="First chunk content for engineering.",
            metadata_json={"section": "intro"},
        ),
        DBDocumentChunk(
            chunk_id="doc_xyz_1",
            document_id="doc_xyz",
            tenant_id="tenant_1",
            chunk_index=1,
            content_hash="c_hash_1",
            content="Second chunk content for product.",
            metadata_json={"section": "features"},
        ),
    ]
    chunk_repo.create_batch(chunks)

    fetched_chunks = chunk_repo.get_by_document("doc_xyz", tenant_id="tenant_1")
    assert len(fetched_chunks) == 2
    assert fetched_chunks[0].chunk_id == "doc_xyz_0"

    # Indexed chunks query
    indexed_chunks = chunk_repo.get_indexed_chunks(tenant_id="tenant_1")
    assert len(indexed_chunks) == 2


def test_audit_repository(db_session):
    tenant_repo = SQLTenantRepository(db_session)
    tenant_repo.create(Tenant(tenant_id="tenant_1", name="Tenant 1"))

    audit_repo = SQLAuditRepository(db_session)

    event = audit_repo.record_event({
        "event_id": "evt_123",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_id": "tenant_1",
        "user_id": "usr_001",
        "event_type": "DOCUMENT_ACCESS",
        "action": "read",
        "result": "success",
        "document_id": "doc_xyz",
        "details": {"query": "What is company policy?"},
    })
    assert event.event_id == "evt_123"

    events = audit_repo.query_events(tenant_id="tenant_1", user_id="usr_001")
    assert len(events) == 1
    assert events[0].action == "read"
    assert events[0].result == "success"
