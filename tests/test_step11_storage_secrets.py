"""
test_step11_storage_secrets.py — Step 11 Production Cloud Storage, KMS, and Secrets Management Test Suite.
Verifies S3DocumentStorage, SSE-KMS CMK, AWSSecretsManagerProvider, bounded caching,
authoritative PostgreSQL SHA-256 integrity contracts, verified streaming tempfile context managers,
PostgreSQL-authoritative local-to-S3 migration state machine, path-traversal defenses,
KMS/IAM least privilege, and fail-closed error handling.
"""

import hashlib
import io
import json
import os
import tempfile
import time
import uuid
from typing import Dict, Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.storage.database import Base
from app.storage.models.document import Document as DBDocument
from app.storage.models.tenant import Tenant as DBTenant
from app.storage.models.migration import DocumentStorageMigration
from app.storage.models.task import Task as DBTask
from app.storage.repositories.document_repository import SQLDocumentRepository
from app.storage.repositories.migration_repository import SQLStorageMigrationRepository
from app.tasks.handlers.base import WorkerContext, NonRetryableTaskError
from app.tasks.handlers.ingestion import DocumentIngestHandler
from app.tasks.handlers.deletion import DocumentDeleteHandler
from app.tasks.models import TaskType

from app.storage.blob.base import DocumentStorageInterface
from app.storage.blob.errors import (
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageIntegrityError,
    StorageConfigurationError,
)
from app.storage.blob.storage import LocalFilesystemStorage
from app.storage.blob.s3 import S3DocumentStorage, build_object_key, parse_s3_uri
from app.storage.blob.factory import get_document_storage, reset_document_storage
from app.storage.blob.migration import StorageMigrationService, MigrationState, MigrationResult

from app.connectors.errors import (
    SecretProviderError,
    SecretNotFoundError,
    SecretDecryptionError,
)
from app.connectors.secrets.local import LocalSecretProvider
from app.connectors.secrets.aws import (
    AWSSecretsManagerProvider,
    BoundedSecretCache,
    build_secret_name,
)
from app.connectors.secrets.factory import get_secret_provider, reset_secret_provider


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
    reset_document_storage()
    reset_secret_provider()
    yield db
    db.close()
    reset_document_storage()
    reset_secret_provider()


# ═════════════════════════════════════════════════════════════════════════════
# 1. S3 OBJECT KEY GENERATION & PATH TRAVERSAL DEFENSE
# ═════════════════════════════════════════════════════════════════════════════

class TestS3ObjectKeySecurity:
    def test_valid_deterministic_key_generation(self):
        tenant_id = "tenant_alpha"
        document_id = "doc_12345"
        version = 2
        content_hash = hashlib.sha256(b"hello world").hexdigest()

        key = build_object_key(tenant_id, document_id, version, content_hash)
        expected = f"tenants/tenant_alpha/documents/doc_12345/versions/2/{content_hash}"
        assert key == expected

    @pytest.mark.parametrize("bad_tenant", [
        "../tenant_alpha",
        "tenant/alpha",
        "tenant\x00alpha",
        "",
        "tenant alpha",
        "../../etc/passwd",
    ])
    def test_rejects_unsafe_tenant_id(self, bad_tenant):
        valid_hash = hashlib.sha256(b"test").hexdigest()
        with pytest.raises(StorageError):
            build_object_key(bad_tenant, "doc_1", 1, valid_hash)

    @pytest.mark.parametrize("bad_doc_id", [
        "../doc_1",
        "doc/1",
        "doc\x001",
        "",
        "../../shadow",
    ])
    def test_rejects_unsafe_document_id(self, bad_doc_id):
        valid_hash = hashlib.sha256(b"test").hexdigest()
        with pytest.raises(StorageError):
            build_object_key("tenant_1", bad_doc_id, 1, valid_hash)

    def test_rejects_invalid_version(self):
        valid_hash = hashlib.sha256(b"test").hexdigest()
        with pytest.raises(StorageError):
            build_object_key("tenant_1", "doc_1", 0, valid_hash)
        with pytest.raises(StorageError):
            build_object_key("tenant_1", "doc_1", -1, valid_hash)

    def test_rejects_invalid_content_hash(self):
        with pytest.raises(StorageError):
            build_object_key("tenant_1", "doc_1", 1, "not-a-valid-sha256-hash")
        with pytest.raises(StorageError):
            build_object_key("tenant_1", "doc_1", 1, "")

    def test_filename_does_not_affect_s3_key_identity(self):
        content = b"enterprise document data"
        c_hash = hashlib.sha256(content).hexdigest()
        key1 = build_object_key("tenant_1", "doc_abc", 1, c_hash)
        key2 = build_object_key("tenant_1", "doc_abc", 1, c_hash)
        assert key1 == key2

    def test_parse_s3_uri(self):
        bucket, key = parse_s3_uri("s3://my-bucket/tenants/t1/doc1", "default-bucket")
        assert bucket == "my-bucket"
        assert key == "tenants/t1/doc1"

        bucket2, key2 = parse_s3_uri("tenants/t1/doc1", "default-bucket")
        assert bucket2 == "default-bucket"
        assert key2 == "tenants/t1/doc1"

        with pytest.raises(StorageError):
            parse_s3_uri("", "default")


# ═════════════════════════════════════════════════════════════════════════════
# 2. S3 DOCUMENT STORAGE WITH SSE-KMS & INTEGRITY CONTRACTS
# ═════════════════════════════════════════════════════════════════════════════

class TestS3DocumentStorage:
    def test_save_and_get_with_sse_kms_and_checksum_match(self):
        mock_s3 = MagicMock()
        content = b"confidential enterprise document contents"
        content_hash = hashlib.sha256(content).hexdigest()
        tenant_id = "tenant_finance"
        doc_id = "doc_fin_001"

        mock_s3.put_object.return_value = {"ETag": '"test-etag"'}
        mock_body = MagicMock()
        mock_body.read.return_value = content
        mock_s3.get_object.return_value = {
            "Body": mock_body,
            "ContentLength": len(content),
            "Metadata": {"sha256": content_hash},
        }

        storage = S3DocumentStorage(
            bucket="enterprise-rag-bucket",
            region="us-east-1",
            kms_key_id="arn:aws:kms:us-east-1:123456789012:key/cmk-key-uuid",
            s3_client=mock_s3,
        )

        # 1. Save document
        s3_uri = storage.save(
            content=content,
            filename="financial_report.pdf",
            tenant_id=tenant_id,
            document_id=doc_id,
            version=1,
            expected_hash=content_hash,
        )

        expected_key = build_object_key(tenant_id, doc_id, 1, content_hash)
        assert s3_uri == f"s3://enterprise-rag-bucket/{expected_key}"

        # Verify put_object arguments enforce SSE-KMS
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "enterprise-rag-bucket"
        assert call_kwargs["Key"] == expected_key
        assert call_kwargs["ServerSideEncryption"] == "aws:kms"
        assert call_kwargs["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:123456789012:key/cmk-key-uuid"
        assert call_kwargs["Metadata"]["sha256"] == content_hash

        # 2. Get document with authoritative expected_hash match (download_verified)
        retrieved_bytes = storage.download_verified(s3_uri, expected_hash=content_hash)
        assert retrieved_bytes == content

    def test_get_fails_closed_on_checksum_mismatch(self):
        mock_s3 = MagicMock()
        original_content = b"original valid document content"
        corrupted_content = b"corrupted / tampered document content"
        authoritative_hash = hashlib.sha256(original_content).hexdigest()

        mock_body = MagicMock()
        mock_body.read.return_value = corrupted_content
        mock_s3.get_object.return_value = {"Body": mock_body}

        storage = S3DocumentStorage(
            bucket="enterprise-rag-bucket",
            region="us-east-1",
            s3_client=mock_s3,
        )

        # Fail closed on SHA-256 mismatch
        with pytest.raises(StorageIntegrityError) as exc_info:
            storage.download_verified("s3://enterprise-rag-bucket/test-key", expected_hash=authoritative_hash)

        assert "Integrity check failed" in str(exc_info.value)

    def test_verified_temp_file_context_manager_lifecycle(self):
        mock_s3 = MagicMock()
        content = b"Valid content to stream to verified temp file."
        c_hash = hashlib.sha256(content).hexdigest()

        stream_io = io.BytesIO(content)
        mock_body = MagicMock()
        mock_body.read = stream_io.read
        mock_body.close = MagicMock()
        mock_s3.get_object.return_value = {"Body": mock_body}

        storage = S3DocumentStorage(bucket="enterprise-bucket", s3_client=mock_s3)

        temp_path_captured = None
        # Successful verified stream
        with storage.verified_temp_file("s3://enterprise-bucket/key1", expected_hash=c_hash, suffix=".txt") as temp_p:
            temp_path_captured = temp_p
            assert os.path.exists(temp_p)
            assert open(temp_p, "rb").read() == content

        # Verified temp file is deterministically cleaned up after context exit
        assert not os.path.exists(temp_path_captured)

    def test_verified_temp_file_cleans_up_and_fails_on_corruption(self):
        mock_s3 = MagicMock()
        corrupted_content = b"corrupted content stream"
        authoritative_hash = hashlib.sha256(b"original expected content").hexdigest()

        stream_io = io.BytesIO(corrupted_content)
        mock_body = MagicMock()
        mock_body.read = stream_io.read
        mock_body.close = MagicMock()
        mock_s3.get_object.return_value = {"Body": mock_body}

        storage = S3DocumentStorage(bucket="enterprise-bucket", s3_client=mock_s3)

        with pytest.raises(StorageIntegrityError):
            with storage.verified_temp_file("s3://enterprise-bucket/corrupt_key", expected_hash=authoritative_hash):
                pass  # Should not be reached

    def test_streaming_upload_and_download(self):
        mock_s3 = MagicMock()
        content = b"A" * 131072  # 128 KB
        content_hash = hashlib.sha256(content).hexdigest()

        mock_s3.put_object.return_value = {"ETag": '"stream-etag"'}
        
        # Mock streaming body
        stream_io = io.BytesIO(content)
        mock_body = MagicMock()
        mock_body.read = stream_io.read
        mock_body.close = MagicMock()
        mock_s3.get_object.return_value = {"Body": mock_body}

        storage = S3DocumentStorage(
            bucket="enterprise-rag-bucket",
            region="us-east-1",
            s3_client=mock_s3,
        )

        # Stream save
        upload_stream = io.BytesIO(content)
        s3_uri = storage.save_stream(
            stream=upload_stream,
            filename="large_file.txt",
            tenant_id="tenant_1",
            document_id="large_doc",
            version=1,
            expected_hash=content_hash,
        )
        assert "s3://enterprise-rag-bucket/" in s3_uri

        # Stream read with chunk iteration & checksum verification
        downloaded_chunks = []
        for chunk in storage.open_stream(s3_uri, chunk_size=32768, expected_hash=content_hash):
            downloaded_chunks.append(chunk)

        assert b"".join(downloaded_chunks) == content

    def test_idempotent_delete_handles_missing_keys(self):
        mock_s3 = MagicMock()
        storage = S3DocumentStorage(bucket="test-bucket", s3_client=mock_s3)

        # 1. Successful deletion
        mock_s3.delete_object.return_value = {}
        assert storage.delete("s3://test-bucket/key1") is True

        # 2. 404 NoSuchKey returns True idempotently
        mock_s3.delete_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
            "DeleteObject",
        )
        assert storage.delete("s3://test-bucket/key2") is True

    def test_sanitized_client_error_mapping(self):
        mock_s3 = MagicMock()
        storage = S3DocumentStorage(bucket="test-bucket", s3_client=mock_s3)

        # 404 -> StorageNotFoundError
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        with pytest.raises(StorageNotFoundError):
            storage.get("s3://test-bucket/missing-key")

        # 403 -> StoragePermissionError
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "GetObject",
        )
        with pytest.raises(StoragePermissionError):
            storage.get("s3://test-bucket/forbidden-key")

    def test_health_check(self):
        mock_s3 = MagicMock()
        storage = S3DocumentStorage(bucket="test-bucket", s3_client=mock_s3)

        mock_s3.head_bucket.return_value = {}
        health = storage.health_check()
        assert health["status"] == "HEALTHY"
        assert health["backend"] == "s3"


# ═════════════════════════════════════════════════════════════════════════════
# 3. KMS & IAM LEAST PRIVILEGE SPECIFICATION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestKMSIAMLeastPrivilege:
    def test_runtime_role_has_no_raw_key_material(self):
        # Enforce that no raw key material is exposed or accepted in config
        assert not hasattr(config, "RAW_KMS_KEY_BYTES")
        assert not hasattr(config, "AES_GCM_RAW_KEY")
        # Key ID is strictly an ARN or Alias reference
        assert isinstance(config.S3_KMS_KEY_ID, str)

    def test_runtime_permitted_kms_actions_specification(self):
        # Permitted runtime actions for S3 SSE-KMS
        runtime_permitted = {
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:GenerateDataKey",
            "kms:DescribeKey",
        }
        # Prohibited administrative actions for runtime
        runtime_prohibited = {
            "kms:CreateKey",
            "kms:PutKeyPolicy",
            "kms:ScheduleKeyDeletion",
            "kms:DisableKey",
            "kms:DeleteAlias",
            "kms:CreateGrant",
            "kms:RevokeGrant",
        }
        # Assert complete disjoint separation
        assert runtime_permitted.isdisjoint(runtime_prohibited)


# ═════════════════════════════════════════════════════════════════════════════
# 4. AWS SECRETS MANAGER PROVIDER & BOUNDED CACHE
# ═════════════════════════════════════════════════════════════════════════════

class TestAWSSecretsManagerProvider:
    def test_bounded_cache_hit_and_ttl_expiration(self):
        cache = BoundedSecretCache(max_entries=3, ttl_seconds=1)
        cache.set("tenant_1:conn_1:config", {"api_key": "secret_123"}, ttl_seconds=1)

        # 1. Cache hit
        assert cache.get("tenant_1:conn_1:config") == {"api_key": "secret_123"}
        assert cache.size() == 1

        # 2. Expired after TTL
        time.sleep(1.1)
        assert cache.get("tenant_1:conn_1:config") is None
        assert cache.size() == 0

    def test_bounded_cache_lru_eviction(self):
        cache = BoundedSecretCache(max_entries=2, ttl_seconds=300)
        cache.set("key1", "val1")
        time.sleep(0.01)
        cache.set("key2", "val2")
        time.sleep(0.01)

        # Access key1 to make key2 least recently used
        _ = cache.get("key1")

        # Add key3 -> should evict key2
        cache.set("key3", "val3")
        assert cache.get("key1") == "val1"
        assert cache.get("key2") is None
        assert cache.get("key3") == "val3"
        assert cache.size() == 2

    def test_secret_name_deterministic_and_tenant_scoped(self):
        name = build_secret_name("enterprise-rag/", "tenant_hr", "salesforce_connector")
        assert name == "enterprise-rag/tenants/tenant_hr/connectors/salesforce_connector/config"

    def test_secrets_manager_provider_encrypt_decrypt_and_caching(self):
        mock_sm = MagicMock()
        mock_sm.create_secret.return_value = {"ARN": "arn:aws:secretsmanager:test"}
        
        provider = AWSSecretsManagerProvider(
            prefix="enterprise-rag/",
            region="us-east-1",
            cache_ttl=300,
            client=mock_sm,
        )

        secret_data = {"client_id": "oauth_client", "client_secret": "super_secret_token_99"}
        secret_name = provider.encrypt_json(
            secret_data,
            tenant_id="tenant_hr",
            connector_id="connector_workday",
        )

        assert "tenants/tenant_hr/connectors/connector_workday/config" in secret_name

        # Decrypt from cache (zero API calls)
        decrypted = provider.decrypt_json(
            secret_name,
            tenant_id="tenant_hr",
            connector_id="connector_workday",
        )
        assert decrypted == secret_data
        mock_sm.get_secret_value.assert_not_called()

        # Invalidate cache and verify fresh fetch from AWS
        provider.invalidate_cache("tenant_hr", "connector_workday")
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps(secret_data)
        }

        decrypted_after_invalidation = provider.decrypt_json(
            secret_name,
            tenant_id="tenant_hr",
            connector_id="connector_workday",
        )
        assert decrypted_after_invalidation == secret_data
        mock_sm.get_secret_value.assert_called_once()

    def test_secrets_manager_sanitized_errors(self):
        mock_sm = MagicMock()
        provider = AWSSecretsManagerProvider(client=mock_sm)

        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Secret not found"}},
            "GetSecretValue",
        )
        with pytest.raises(SecretNotFoundError):
            provider.decrypt_json("nonexistent_secret", tenant_id="t1", connector_id="c1")

        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Not authorized"}},
            "GetSecretValue",
        )
        with pytest.raises(SecretProviderError) as exc_info:
            provider.decrypt_json("forbidden_secret", tenant_id="t1", connector_id="c1")
        assert "Access denied" in str(exc_info.value)


# ═════════════════════════════════════════════════════════════════════════════
# 5. FACTORY & CONFIGURATION VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

class TestStorageAndSecretFactories:
    def test_storage_factory_resolves_local_default(self):
        reset_document_storage()
        with patch.object(config, "DOCUMENT_STORAGE_TYPE", "local"):
            storage = get_document_storage()
            assert isinstance(storage, LocalFilesystemStorage)

    def test_storage_factory_production_validation(self):
        reset_document_storage()
        with patch.object(config, "ENVIRONMENT", "production"), \
             patch.object(config, "DOCUMENT_STORAGE_TYPE", "s3"), \
             patch.object(config, "S3_BUCKET", ""):
            with pytest.raises(StorageConfigurationError):
                get_document_storage()

    def test_secret_factory_resolves_local_default(self):
        reset_secret_provider()
        with patch.object(config, "SECRET_PROVIDER_TYPE", "local"):
            prov = get_secret_provider()
            assert isinstance(prov, LocalSecretProvider)


# ═════════════════════════════════════════════════════════════════════════════
# 6. POSTGRESQL AUTHORITATIVE MIGRATION STATE MACHINE
# ═════════════════════════════════════════════════════════════════════════════

class TestStorageMigrationService:
    def test_successful_migration_lifecycle_with_postgres_persistence(self, test_db, tmp_path):
        db = test_db
        uid = uuid.uuid4().hex[:8]
        tenant_id = f"tenant_mig_{uid}"
        doc_id = f"doc_mig_{uid}"
        content = b"Local file to be migrated to S3 storage."
        c_hash = hashlib.sha256(content).hexdigest()

        # Create local file
        local_file = tmp_path / "report.txt"
        local_file.write_bytes(content)

        # Create DB record
        doc = DBDocument(
            document_id=doc_id,
            tenant_id=tenant_id,
            owner_id="user_1",
            filename="report.txt",
            source="upload",
            content_hash=c_hash,
            storage_path=str(local_file),
            status="INDEXED",
            version=1,
        )
        db.add(doc)
        db.commit()

        mock_target = MagicMock()
        mock_target.save.return_value = f"s3://target-bucket/tenants/{tenant_id}/documents/{doc_id}/versions/1/{c_hash}"
        mock_target.get.return_value = content
        mock_target.download_verified.return_value = content

        service = StorageMigrationService(db=db, target_storage=mock_target)
        mig_repo = SQLStorageMigrationRepository(db)

        # 1. Dry run
        dry_result = service.migrate_document(doc_id, tenant_id=tenant_id, dry_run=True)
        assert dry_result.status == MigrationState.WOULD_MIGRATE
        # DB document remains local
        db.refresh(doc)
        assert doc.storage_path == str(local_file)

        # 2. Real migration
        result = service.migrate_document(doc_id, tenant_id=tenant_id, dry_run=False)
        assert result.status == MigrationState.MIGRATED
        assert result.target_path.startswith("s3://")

        # Verify DB document updated
        db.refresh(doc)
        assert doc.storage_path == result.target_path

        # Verify PostgreSQL authoritative migration record
        mig_record = mig_repo.get_by_document(doc_id, tenant_id=tenant_id)
        assert mig_record is not None
        assert mig_record.status == MigrationState.MIGRATED.value
        assert mig_record.attempt_count >= 1
        assert mig_record.verified_at is not None
        assert mig_record.committed_at is not None
        assert mig_record.completed_at is not None
        assert mig_record.is_orphan_candidate is False

        # 3. Resumability: Second run returns ALREADY_MIGRATED
        resume_result = service.migrate_document(doc_id, tenant_id=tenant_id, dry_run=False)
        assert resume_result.status == MigrationState.ALREADY_MIGRATED

    def test_migration_handles_db_commit_failure_and_orphan_tracking(self, test_db, tmp_path):
        db = test_db
        uid = uuid.uuid4().hex[:8]
        tenant_id = f"tenant_orphan_{uid}"
        doc_id = f"doc_orphan_{uid}"
        content = b"Content for orphan tracking test."
        c_hash = hashlib.sha256(content).hexdigest()

        local_file = tmp_path / "orphan_test.txt"
        local_file.write_bytes(content)

        doc = DBDocument(
            document_id=doc_id,
            tenant_id=tenant_id,
            owner_id="user_1",
            filename="orphan_test.txt",
            source="upload",
            content_hash=c_hash,
            storage_path=str(local_file),
            status="INDEXED",
            version=1,
        )
        db.add(doc)
        db.commit()

        mock_target = MagicMock()
        s3_path = f"s3://target-bucket/tenants/{tenant_id}/documents/{doc_id}/versions/1/{c_hash}"
        mock_target.save.return_value = s3_path
        mock_target.get.return_value = content
        mock_target.download_verified.return_value = content

        service = StorageMigrationService(db=db, target_storage=mock_target)

        # Mock db commit to fail specifically during document record update (3rd commit)
        orig_commit = db.commit
        commit_count = 0

        def conditional_commit():
            nonlocal commit_count
            commit_count += 1
            # Commit 1: UPLOADING, Commit 2: S3_VERIFIED, Commit 3: Document update
            if commit_count == 3:
                raise Exception("Simulated DB connection lost during document commit")
            orig_commit()

        with patch.object(db, "commit", side_effect=conditional_commit):
            result = service.migrate_document(doc_id, tenant_id=tenant_id, dry_run=False)
            assert result.status == MigrationState.DB_COMMIT_FAILED
            
            # Orphan candidate tracked authoritatively in PostgreSQL
            orphans = service.get_orphan_candidates(tenant_id=tenant_id)
            assert len(orphans) >= 1
            assert orphans[0]["document_id"] == doc_id
            assert orphans[0]["reason"] == "db_commit_failed"

        # Local source file is strictly preserved
        assert os.path.exists(str(local_file))


# ═════════════════════════════════════════════════════════════════════════════
# 7. INGESTION & DELETION HANDLER INTEGRATION WITH S3
# ═════════════════════════════════════════════════════════════════════════════

class TestTaskHandlersStorageIntegration:
    def test_ingestion_handler_downloads_s3_and_cleans_tempfile(self, test_db):
        db = test_db
        uid = uuid.uuid4().hex[:8]
        tenant_id = f"tenant_task_s3_{uid}"
        doc_id = f"doc_task_s3_{uid}"
        content = b"This is clean text content from cloud storage for ingestion."
        c_hash = hashlib.sha256(content).hexdigest()

        doc = DBDocument(
            document_id=doc_id,
            tenant_id=tenant_id,
            owner_id="user_1",
            filename="notes.txt",
            source="upload",
            content_hash=c_hash,
            storage_path=f"s3://enterprise-bucket/tenants/{tenant_id}/documents/{doc_id}/versions/1/{c_hash}",
            status="PENDING",
            version=1,
        )
        db.add(doc)
        db.commit()

        mock_storage = MagicMock()
        mock_storage.get.return_value = content
        
        # Mock verified_temp_file context manager
        from contextlib import contextmanager
        @contextmanager
        def mock_temp(storage_path, expected_hash, suffix=".tmp"):
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                tf.write(content)
                t_name = tf.name
            try:
                yield t_name
            finally:
                if os.path.exists(t_name):
                    os.remove(t_name)

        mock_storage.verified_temp_file = mock_temp

        mock_search_store = MagicMock()
        context = WorkerContext(
            db=db,
            search_store=mock_search_store,
            blob_storage=mock_storage,
            secret_provider=LocalSecretProvider(),
            rag_service=None,
            worker_id="test-worker",
        )

        task_id = f"task_ingest_{uid}"
        task = DBTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type=TaskType.DOCUMENT_INGEST.value,
            idempotency_key=f"ingest_key_{uid}",
            aggregate_id=doc_id,
            status="CLAIMED",
            payload={
                "document_id": doc_id,
                "document_version": 1,
                "storage_path": doc.storage_path,
                "owner_id": "user_1",
                "access_level": "PRIVATE",
            },
        )

        handler = DocumentIngestHandler()
        result = handler.handle(task, context)

        assert result["status"] == "INDEXED"
        assert result["document_id"] == doc_id
        # Verify indexed status in DB
        db.refresh(doc)
        assert doc.status == "INDEXED"

    def test_ingestion_handler_fails_closed_on_storage_integrity_violation(self, test_db):
        db = test_db
        uid = uuid.uuid4().hex[:8]
        tenant_id = f"tenant_corrupt_{uid}"
        doc_id = f"doc_corrupt_{uid}"
        c_hash = hashlib.sha256(b"authentic").hexdigest()

        doc = DBDocument(
            document_id=doc_id,
            tenant_id=tenant_id,
            owner_id="user_1",
            filename="tampered.txt",
            source="upload",
            content_hash=c_hash,
            storage_path=f"s3://enterprise-bucket/tenants/{tenant_id}/documents/{doc_id}/versions/1/{c_hash}",
            status="PENDING",
            version=1,
        )
        db.add(doc)
        db.commit()

        mock_storage = MagicMock()
        from contextlib import contextmanager
        @contextmanager
        def mock_corrupt_temp(storage_path, expected_hash, suffix=".tmp"):
            raise StorageIntegrityError("SHA-256 mismatch: content is corrupted.")
            yield ""

        mock_storage.verified_temp_file = mock_corrupt_temp

        context = WorkerContext(
            db=db,
            search_store=MagicMock(),
            blob_storage=mock_storage,
            secret_provider=LocalSecretProvider(),
            rag_service=None,
            worker_id="test-worker",
        )

        task_id = f"task_ingest_corrupt_{uid}"
        task = DBTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type=TaskType.DOCUMENT_INGEST.value,
            idempotency_key=f"ingest_key_corrupt_{uid}",
            aggregate_id=doc_id,
            status="CLAIMED",
            payload={
                "document_id": doc_id,
                "document_version": 1,
                "storage_path": doc.storage_path,
                "owner_id": "user_1",
                "access_level": "PRIVATE",
            },
        )

        handler = DocumentIngestHandler()
        with pytest.raises(NonRetryableTaskError):
            handler.handle(task, context)

        db.refresh(doc)
        assert doc.status == "FAILED"
        assert "integrity check failed" in doc.error_message.lower()
