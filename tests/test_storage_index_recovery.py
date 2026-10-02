"""
test_storage_index_recovery.py — Tests for disaster recovery and rebuilding derived search indexes from PostgreSQL.
"""

import os
import sys
import pytest
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

from app.storage.database import Base
from app.storage.models import Tenant, Document as DBDocument, DocumentChunk as DBDocumentChunk
from app.storage.rebuilder import IndexRebuilderService
from app.storage.vector.base import VectorStoreAdapterInterface
from app.storage.keyword.base import KeywordSearchStoreInterface


@pytest.fixture
def recovery_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()

    # Seed Tenant
    session.add(Tenant(tenant_id="company_a", name="Company A"))

    # Seed 2 INDEXED documents and 1 FAILED document
    session.add(
        DBDocument(
            document_id="doc_indexed_1",
            tenant_id="company_a",
            filename="prod_guide.txt",
            source="upload",
            content_hash="hash_prod_1",
            access_level="PUBLIC",
            status="INDEXED",
        )
    )
    session.add(
        DBDocument(
            document_id="doc_indexed_2",
            tenant_id="company_a",
            filename="security_policy.txt",
            source="upload",
            content_hash="hash_prod_2",
            access_level="ROLE_BASED",
            status="INDEXED",
        )
    )
    session.add(
        DBDocument(
            document_id="doc_failed",
            tenant_id="company_a",
            filename="broken_doc.txt",
            source="upload",
            content_hash="hash_broken",
            access_level="PRIVATE",
            status="FAILED",
        )
    )

    # Add chunks for first indexed doc
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_prod_1",
            document_id="doc_indexed_1",
            tenant_id="company_a",
            chunk_index=0,
            content_hash="c_h_1",
            content="Production Guide: Deployment best practices and rollback procedures.",
            metadata_json={"document_id": "doc_indexed_1", "tenant_id": "company_a"},
        )
    )
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_prod_2",
            document_id="doc_indexed_1",
            tenant_id="company_a",
            chunk_index=1,
            content_hash="c_h_2",
            content="Production Guide: Database migration safety checks and disaster recovery.",
            metadata_json={"document_id": "doc_indexed_1", "tenant_id": "company_a"},
        )
    )

    # Add chunks for second indexed doc
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_sec_1",
            document_id="doc_indexed_2",
            tenant_id="company_a",
            chunk_index=0,
            content_hash="c_h_sec_1",
            content="Security Policy: Zero-trust architecture and role-based access control.",
            metadata_json={"document_id": "doc_indexed_2", "tenant_id": "company_a"},
        )
    )
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_sec_2",
            document_id="doc_indexed_2",
            tenant_id="company_a",
            chunk_index=1,
            content_hash="c_h_sec_2",
            content="Security Policy: Audit logging and token revocation protocols.",
            metadata_json={"document_id": "doc_indexed_2", "tenant_id": "company_a"},
        )
    )

    # Add chunk for failed doc (must NOT be indexed during rebuild)
    session.add(
        DBDocumentChunk(
            chunk_id="chunk_failed_1",
            document_id="doc_failed",
            tenant_id="company_a",
            chunk_index=0,
            content_hash="c_h_f",
            content="Broken content that failed validation.",
            metadata_json={"document_id": "doc_failed", "tenant_id": "company_a"},
        )
    )

    session.commit()
    yield session
    session.close()


def test_index_rebuilder_service(recovery_db):
    mock_vector = MagicMock(spec=VectorStoreAdapterInterface)
    mock_keyword = MagicMock(spec=KeywordSearchStoreInterface)

    rebuilder = IndexRebuilderService(
        db=recovery_db,
        vector_adapter=mock_vector,
        keyword_adapter=mock_keyword,
    )

    result = rebuilder.rebuild_all(tenant_id="company_a")

    assert result["status"] == "success"
    assert result["chunks_indexed"] == 4  # 4 chunks across 2 'INDEXED' docs (failed doc skipped)
    assert result["bm25_rebuilt_count"] == 4
    assert result["vector_rebuilt_count"] == 4

    # Verify vector adapter received exactly 4 LangChain documents
    mock_vector.add_documents.assert_called_once()
    added_docs = mock_vector.add_documents.call_args[0][0]
    assert len(added_docs) == 4
    assert any("Deployment best practices" in d.page_content for d in added_docs)
    assert any("Zero-trust architecture" in d.page_content for d in added_docs)

    # Verify keyword adapter received exactly 4 document dicts
    mock_keyword.index_documents.assert_called_once()
    indexed_docs = mock_keyword.index_documents.call_args[0][0]
    assert len(indexed_docs) == 4
    indexed_chunk_ids = {d["id"] for d in indexed_docs}
    assert indexed_chunk_ids == {"chunk_prod_1", "chunk_prod_2", "chunk_sec_1", "chunk_sec_2"}
    assert "chunk_failed_1" not in indexed_chunk_ids

