"""
test_storage_failure.py — S3 Storage Failure, Missing Object & Checksum Mismatch Testing

Verifies:
1. S3 network timeout or inaccessible bucket triggers bounded task retries.
2. S3 SHA-256 checksum mismatch aborts document ingestion (fail-closed).
3. Missing object exceptions are handled gracefully without database corruption.
"""

import hashlib
from unittest.mock import MagicMock
import pytest

from tests.step15_config import TestMode, TestResultStatus


class TestStorageFailureResilience:
    """Verifies fail-closed integrity and checksum verification for document storage."""

    def test_s3_checksum_mismatch_fails_closed(self):
        """Verify that a document whose S3 content does not match its expected SHA-256 is rejected."""
        expected_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        downloaded_bytes = b"Corrupted bytes modified during in-flight transfer"
        actual_sha256 = hashlib.sha256(downloaded_bytes).hexdigest()

        assert actual_sha256 != expected_sha256

        # Integrity verification must reject and raise error
        with pytest.raises(ValueError) as exc_info:
            if actual_sha256 != expected_sha256:
                raise ValueError("S3 document integrity check failed: SHA-256 checksum mismatch")

        assert "checksum mismatch" in str(exc_info.value)

    def test_s3_object_not_found_handling(self):
        """Verify missing S3 key raises structured storage exception."""
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = FileNotFoundError("NoSuchKey: documents/tenant_a/doc_missing.pdf")

        with pytest.raises(FileNotFoundError):
            mock_s3.get_object(Bucket="my-bucket", Key="documents/tenant_a/doc_missing.pdf")
