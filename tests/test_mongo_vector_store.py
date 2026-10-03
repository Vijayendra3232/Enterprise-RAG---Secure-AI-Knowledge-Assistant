"""
test_mongo_vector_store.py — Comprehensive unit & integration tests for MongoVectorStore.
Verifies connection, 384-dimensional vector operations, idempotent indexing,
tenant isolation, deletion, metadata updates, and offline resilience using fakes/mocks.
"""

import pytest
from unittest.mock import MagicMock, patch
from app.storage.search.mongo_store import MongoVectorStore
from app.storage.search.models import ChunkPayload
from app.retrieval.filters import MetadataFilter


class FakeMongoCollection:
    """In-memory fake MongoDB collection simulating pymongo operations."""
    def __init__(self):
        self.docs = {}

    def replace_one(self, filter_doc, replacement, upsert=False):
        key = (filter_doc.get("chunk_id"), filter_doc.get("tenant_id"))
        self.docs[key] = dict(replacement)
        return MagicMock(matched_count=1, modified_count=1, upserted_count=1 if key not in self.docs else 0)

    def bulk_write(self, operations):
        inserted = 0
        modified = 0
        upserted = 0
        for op in operations:
            key = (op._filter.get("chunk_id"), op._filter.get("tenant_id"))
            if key in self.docs:
                modified += 1
            else:
                upserted += 1
            self.docs[key] = dict(op._doc)
        return MagicMock(inserted_count=inserted, modified_count=modified, upserted_count=upserted)

    def update_one(self, filter_doc, update_doc):
        key = (filter_doc.get("chunk_id"), filter_doc.get("tenant_id"))
        if key in self.docs:
            if "$set" in update_doc:
                for k, v in update_doc["$set"].items():
                    self.docs[key][k] = v
            return MagicMock(matched_count=1, modified_count=1)
        return MagicMock(matched_count=0, modified_count=0)

    def delete_one(self, filter_doc):
        key = (filter_doc.get("chunk_id"), filter_doc.get("tenant_id"))
        if key in self.docs:
            del self.docs[key]
            return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def delete_many(self, filter_doc):
        to_delete = []
        doc_id = filter_doc.get("document_id")
        tenant_id = filter_doc.get("tenant_id")
        chunk_ids = filter_doc.get("chunk_id", {}).get("$in") if isinstance(filter_doc.get("chunk_id"), dict) else None

        for key, doc in list(self.docs.items()):
            match = True
            if tenant_id and doc.get("tenant_id") != tenant_id:
                match = False
            if doc_id and doc.get("document_id") != doc_id:
                match = False
            if chunk_ids and doc.get("chunk_id") not in chunk_ids:
                match = False
            if match:
                to_delete.append(key)

        for key in to_delete:
            del self.docs[key]
        return MagicMock(deleted_count=len(to_delete))

    def find(self, filter_doc=None):
        filter_doc = filter_doc or {}
        tenant_id = filter_doc.get("tenant_id")
        doc_id = filter_doc.get("document_id")

        results = []
        for doc in self.docs.values():
            if tenant_id and doc.get("tenant_id") != tenant_id:
                continue
            if doc_id and doc.get("document_id") != doc_id:
                continue
            results.append(dict(doc))
        return FakeCursor(results)

    def count_documents(self, filter_doc=None):
        return len(self.find(filter_doc).items)

    def aggregate(self, pipeline):
        filter_doc = {}
        for stage in pipeline:
            if "$vectorSearch" in stage:
                filter_doc = stage["$vectorSearch"].get("filter", {})
        return self.find(filter_doc).items


class FakeCursor:
    def __init__(self, items):
        self.items = items

    def limit(self, n):
        return FakeCursor(self.items[:n])

    def __iter__(self):
        return iter(self.items)


@pytest.fixture
def fake_mongo_store():
    store = MongoVectorStore(mongo_uri="mongodb://localhost:27017")
    fake_coll = FakeMongoCollection()

    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.__getitem__.return_value = fake_coll
    mock_db.command.return_value = {"ok": 1}
    mock_client.__getitem__.return_value = mock_db
    mock_client.admin.command.return_value = {"ok": 1}
    store._client = mock_client
    return store, fake_coll


def test_mongo_vector_store_initialization():
    store = MongoVectorStore(
        mongo_uri="mongodb://fake:27017",
        db_name="enterprise_rag",
        collection_name="chunk_vectors",
        index_name="vector_index",
        configured_dimension=384,
    )
    assert store.configured_dimension == 384
    assert store.db_name == "enterprise_rag"
    assert store.collection_name == "chunk_vectors"
    assert store.index_name == "vector_index"


def test_mongo_config_exposure():
    from app import config
    assert hasattr(config, "MONGODB_URI")
    assert hasattr(config, "MONGODB_DATABASE")
    assert hasattr(config, "MONGODB_COLLECTION")
    assert hasattr(config, "MONGODB_VECTOR_INDEX")
    assert config.MONGODB_DATABASE == "enterprise_rag"
    assert config.MONGODB_COLLECTION == "chunk_vectors"
    assert config.MONGODB_VECTOR_INDEX == "vector_index"


def test_mongodb_srv_uri_and_timeout_configuration():
    srv_uri = "mongodb+srv://user:pass@cluster0.example.mongodb.net/"
    with patch("pymongo.MongoClient") as mock_mongo_cls:
        mock_instance = MagicMock()
        mock_mongo_cls.return_value = mock_instance
        store = MongoVectorStore(mongo_uri=srv_uri)
        client = store._get_client()
        assert client is mock_instance
        mock_mongo_cls.assert_called_once_with(
            srv_uri,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
        )


def test_health_check_healthy(fake_mongo_store):
    store, _ = fake_mongo_store
    health = store.health_check()
    assert health.status == "HEALTHY"
    assert health.backend == "mongodb_atlas"
    assert health.configured_dimension == 384
    assert health.cluster_healthy is True
    # Ensure command("ping") was executed against the configured database
    db_mock = store._client[store.db_name]
    db_mock.command.assert_called_with("ping")


def test_health_check_unhealthy_when_offline():
    store = MongoVectorStore(mongo_uri="")
    health = store.health_check()
    assert health.status == "UNHEALTHY"
    assert health.backend == "mongodb_atlas"
    assert health.cluster_healthy is False
    err_msg = health.details.get("error_message", "").lower()
    assert "missing" in err_msg or "uninitialized" in err_msg


def test_index_chunk_and_idempotency(fake_mongo_store):
    store, fake_coll = fake_mongo_store
    dummy_embedding = [0.1] * 384

    payload = ChunkPayload(
        chunk_id="chunk_1",
        document_id="doc_1",
        tenant_id="tenant_a",
        document_version=1,
        content="Security policy SOP guidelines.",
        content_hash="hash123",
        embedding=dummy_embedding,
        metadata={"source": "sop.txt"},
    )

    # 1. Index first time
    success = store.index_chunk(payload)
    assert success is True
    assert len(fake_coll.docs) == 1

    # 2. Re-index same chunk_id with updated version (idempotent upsert)
    payload.document_version = 2
    success_2 = store.index_chunk(payload)
    assert success_2 is True
    assert len(fake_coll.docs) == 1  # No duplicate record created
    indexed_doc = fake_coll.docs[("chunk_1", "tenant_a")]
    assert indexed_doc["version"] == 2


def test_bulk_index_chunks(fake_mongo_store):
    store, fake_coll = fake_mongo_store
    payloads = [
        ChunkPayload(
            chunk_id=f"chunk_{i}",
            document_id="doc_bulk",
            tenant_id="tenant_a",
            document_version=1,
            content=f"Content batch item {i}",
            content_hash=f"hash_{i}",
            embedding=[0.05 * i] * 384,
        )
        for i in range(5)
    ]

    count = store.index_chunks(payloads)
    assert count == 5
    assert store.count("tenant_a") == 5


def test_vector_search_and_tenant_isolation(fake_mongo_store):
    store, fake_coll = fake_mongo_store
    vec_a = [0.1] * 384
    vec_b = [0.9] * 384

    # Seed vectors for two different tenants
    store.index_chunk(ChunkPayload(chunk_id="c_tenant_a", document_id="d1", tenant_id="tenant_a", document_version=1, content="Tenant A policy", content_hash="h1", embedding=vec_a))
    store.index_chunk(ChunkPayload(chunk_id="c_tenant_b", document_id="d2", tenant_id="tenant_b", document_version=1, content="Tenant B secrets", content_hash="h2", embedding=vec_b))

    # Query scoped to tenant_a
    filters_a = MetadataFilter(tenant_id="tenant_a")
    results_a = store.vector_search(query_vector=vec_a, top_k=10, filters=filters_a)
    assert len(results_a) == 1
    assert results_a[0].chunk_id == "c_tenant_a"

    # Query scoped to tenant_b
    filters_b = MetadataFilter(tenant_id="tenant_b")
    results_b = store.vector_search(query_vector=vec_b, top_k=10, filters=filters_b)
    assert len(results_b) == 1
    assert results_b[0].chunk_id == "c_tenant_b"


def test_permission_change_without_reembedding(fake_mongo_store):
    store, fake_coll = fake_mongo_store
    vec = [0.2] * 384
    payload = ChunkPayload(
        chunk_id="c_perm",
        document_id="d_perm",
        tenant_id="tenant_a",
        document_version=1,
        content="Access control SOP",
        content_hash="h_perm",
        embedding=vec,
        metadata={"access_level": "PRIVATE"},
    )
    store.index_chunk(payload)

    # Update metadata in-place
    new_meta = {"access_level": "ROLE_BASED", "allowed_roles": ["ADMIN"]}
    updated = store.update_chunk_metadata(chunk_id="c_perm", metadata=new_meta, tenant_id="tenant_a")
    assert updated is True

    indexed_doc = fake_coll.docs[("c_perm", "tenant_a")]
    assert indexed_doc["metadata"] == new_meta
    assert indexed_doc["embedding"] == vec  # Embedding unchanged


def test_delete_document_vectors(fake_mongo_store):
    store, fake_coll = fake_mongo_store
    vec = [0.1] * 384
    store.index_chunk(ChunkPayload(chunk_id="c1", document_id="doc_del", tenant_id="tenant_a", document_version=1, content="c1", content_hash="h1", embedding=vec))
    store.index_chunk(ChunkPayload(chunk_id="c2", document_id="doc_del", tenant_id="tenant_a", document_version=1, content="c2", content_hash="h2", embedding=vec))
    store.index_chunk(ChunkPayload(chunk_id="c3", document_id="doc_keep", tenant_id="tenant_a", document_version=1, content="c3", content_hash="h3", embedding=vec))

    assert store.count("tenant_a") == 3

    deleted_count = store.delete_document_vectors("doc_del", tenant_id="tenant_a")
    assert deleted_count == 2
    assert store.count("tenant_a") == 1


def test_empty_search_results_and_malformed_vectors(fake_mongo_store):
    store, _ = fake_mongo_store

    # 1. Empty query vector
    res_empty = store.vector_search(query_vector=[], top_k=5)
    assert res_empty == []

    # 2. Wrong dimension query vector (e.g. 128 instead of 384)
    res_wrong_dim = store.vector_search(query_vector=[0.1] * 128, top_k=5)
    assert res_wrong_dim == []

    # 3. Empty text query for similarity search
    res_empty_text = store.similarity_search(query="")
    assert res_empty_text == []
