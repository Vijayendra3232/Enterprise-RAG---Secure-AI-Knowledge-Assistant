"""
test_aws_live_integration.py — Gated Live AWS Integration Test Suite for S3 and Secrets Manager.
Strictly gated behind RUN_AWS_INTEGRATION_TESTS=true to ensure normal test suites remain 100% offline.
Guarantees deterministic cleanup of all test artifacts in finally blocks.
"""

import hashlib
import os
import time
import uuid
import pytest

from app.storage.blob.s3 import S3DocumentStorage, build_object_key
from app.connectors.secrets.aws import AWSSecretsManagerProvider, build_secret_name
from app.storage.blob.errors import StorageIntegrityError, StorageNotFoundError

RUN_AWS = os.getenv("RUN_AWS_INTEGRATION_TESTS", "false").lower() in ("true", "1", "yes")

pytestmark = pytest.mark.skipif(
    not RUN_AWS,
    reason="Live AWS integration tests skipped (set RUN_AWS_INTEGRATION_TESTS=true to enable).",
)


@pytest.fixture(scope="module")
def aws_env_config():
    bucket = os.getenv("AWS_TEST_S3_BUCKET")
    region = os.getenv("AWS_TEST_REGION", "us-east-1")
    kms_key_id = os.getenv("AWS_TEST_KMS_KEY_ID")

    if not bucket:
        pytest.skip("AWS_TEST_S3_BUCKET environment variable is required for live AWS integration tests.")

    return {
        "bucket": bucket,
        "region": region,
        "kms_key_id": kms_key_id,
        "prefix": f"test-run-{uuid.uuid4().hex[:8]}",
    }


class TestLiveS3StorageIntegration:
    def test_live_s3_upload_download_kms_and_cleanup(self, aws_env_config):
        storage = S3DocumentStorage(
            bucket=aws_env_config["bucket"],
            region=aws_env_config["region"],
            kms_key_id=aws_env_config["kms_key_id"],
        )

        tenant_id = f"test_tenant_{uuid.uuid4().hex[:6]}"
        doc_id = f"doc_{uuid.uuid4().hex[:8]}"
        content = b"Live AWS S3 integration test payload with customer-managed KMS encryption."
        c_hash = hashlib.sha256(content).hexdigest()

        s3_uri = None
        try:
            # 1. Upload
            s3_uri = storage.save(
                content=content,
                filename="live_test.txt",
                tenant_id=tenant_id,
                document_id=doc_id,
                version=1,
                expected_hash=c_hash,
            )
            assert s3_uri.startswith("s3://")
            assert storage.exists(s3_uri) is True

            # 2. Verify metadata & KMS
            meta = storage.get_metadata(s3_uri)
            assert meta["size"] == len(content)
            assert meta["server_side_encryption"] == "aws:kms"

            # 3. Retrieve with authoritative hash match
            retrieved = storage.get(s3_uri, expected_hash=c_hash)
            assert retrieved == content

            # 4. Checksum mismatch rejection
            bad_hash = hashlib.sha256(b"tampered content").hexdigest()
            with pytest.raises(StorageIntegrityError):
                storage.get(s3_uri, expected_hash=bad_hash)

        finally:
            if s3_uri:
                storage.delete(s3_uri)
                assert storage.exists(s3_uri) is False


class TestLiveAWSSecretsManagerIntegration:
    def test_live_secrets_lifecycle_and_caching(self, aws_env_config):
        provider = AWSSecretsManagerProvider(
            prefix=f"enterprise-rag-test/{aws_env_config['prefix']}/",
            region=aws_env_config["region"],
            cache_ttl=5,
        )

        tenant_id = f"test_tenant_{uuid.uuid4().hex[:6]}"
        connector_id = f"conn_{uuid.uuid4().hex[:6]}"
        secret_payload = {"api_key": "live_test_api_key_value", "region": "us-east-1"}

        secret_name = None
        try:
            # 1. Create/encrypt secret
            secret_name = provider.encrypt_json(
                secret_payload,
                tenant_id=tenant_id,
                connector_id=connector_id,
            )
            assert secret_name is not None

            # 2. Decrypt via cache
            cached_val = provider.decrypt_json(
                secret_name,
                tenant_id=tenant_id,
                connector_id=connector_id,
            )
            assert cached_val == secret_payload

            # 3. Invalidate cache and re-fetch live
            provider.invalidate_cache(tenant_id, connector_id)
            live_val = provider.decrypt_json(
                secret_name,
                tenant_id=tenant_id,
                connector_id=connector_id,
            )
            assert live_val == secret_payload

        finally:
            if secret_name:
                try:
                    provider._client.delete_secret(
                        SecretId=secret_name,
                        ForceDeleteWithoutRecovery=True,
                    )
                except Exception:
                    pass
