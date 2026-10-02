"""
s3.py — Production S3DocumentStorage implementation with SSE-KMS CMK encryption,
deterministic server-controlled object keys, streaming I/O, and authoritative SHA-256 verification.
"""

import hashlib
import io
import logging
import os
import re
import time
from typing import Optional, Dict, Any, Tuple, BinaryIO, Iterator
from urllib.parse import urlparse

import botocore.config
import botocore.exceptions

from app import config
from app.storage.blob.base import DocumentStorageInterface
from app.storage.blob.errors import (
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageIntegrityError,
    StorageConfigurationError,
)
from app.observability.metrics import (
    storage_operations_total,
    storage_duration_seconds,
    storage_bytes_total,
)
from app.observability.tracing import tracer

logger = logging.getLogger(__name__)

# Strict identifier validation patterns
_SAFE_TENANT_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SAFE_DOC_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_SAFE_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


def build_object_key(
    tenant_id: str,
    document_id: str,
    version: int,
    content_hash: str,
) -> str:
    """
    Construct a deterministic, server-controlled, tenant-isolated S3 object key.
    Enforces strict validation against path traversal, null bytes, and malicious characters.
    """
    if not tenant_id or not isinstance(tenant_id, str) or not _SAFE_TENANT_RE.match(tenant_id):
        raise StorageError(f"Invalid or unsafe tenant_id for S3 object key: '{tenant_id}'")

    if not document_id or not isinstance(document_id, str) or not _SAFE_DOC_ID_RE.match(document_id):
        raise StorageError(f"Invalid or unsafe document_id for S3 object key: '{document_id}'")

    if not isinstance(version, int) or version < 1:
        raise StorageError(f"Invalid document version for S3 object key: {version}")

    norm_hash = (content_hash or "").strip().lower()
    if not _SAFE_HASH_RE.match(norm_hash):
        raise StorageError(f"Invalid or unsafe SHA-256 content_hash for S3 object key: '{content_hash}'")

    return f"tenants/{tenant_id}/documents/{document_id}/versions/{version}/{norm_hash}"


def parse_s3_uri(storage_path: str, default_bucket: str) -> Tuple[str, str]:
    """
    Parse an S3 URI (s3://bucket/key) or relative key into (bucket, key).
    """
    if not storage_path or not isinstance(storage_path, str):
        raise StorageError("Empty or invalid storage_path provided.")

    if storage_path.startswith("s3://"):
        parsed = urlparse(storage_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise StorageError(f"Malformed S3 URI: '{storage_path}'")
        return bucket, key

    return default_bucket, storage_path.lstrip("/")


class S3DocumentStorage(DocumentStorageInterface):
    """
    Production-grade Amazon S3 Document Storage adapter.
    Enforces SSE-KMS CMK encryption, deterministic tenant scoping, and PostgreSQL-authoritative checksum validation.
    """

    def __init__(
        self,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        kms_key_id: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        connect_timeout: Optional[int] = None,
        read_timeout: Optional[int] = None,
        s3_client: Optional[Any] = None,
    ):
        self.bucket = (bucket or config.S3_BUCKET).strip()
        self.region = (region or config.S3_REGION).strip()
        self.kms_key_id = (kms_key_id or config.S3_KMS_KEY_ID or "").strip()
        self.endpoint_url = endpoint_url or config.S3_ENDPOINT_URL

        if not self.bucket:
            raise StorageConfigurationError("S3_BUCKET configuration is required for S3DocumentStorage.")

        if s3_client is not None:
            self._client = s3_client
        else:
            import boto3
            boto_config = botocore.config.Config(
                connect_timeout=connect_timeout or config.S3_CONNECT_TIMEOUT,
                read_timeout=read_timeout or config.S3_READ_TIMEOUT,
                retries={"max_attempts": 3, "mode": "standard"},
            )
            self._client = boto3.client(
                "s3",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
                config=boto_config,
            )

    def _map_client_error(self, exc: botocore.exceptions.ClientError, key: str) -> StorageError:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            return StorageNotFoundError(f"Object not found in storage: '{key}'")
        if error_code in ("AccessDenied", "403"):
            return StoragePermissionError(f"Access denied to storage object: '{key}'")
        return StorageError(f"S3 storage operation failed with error code: {error_code}")

    def save(
        self,
        content: bytes,
        filename: str,
        tenant_id: str,
        document_id: Optional[str] = None,
        version: int = 1,
        expected_hash: Optional[str] = None,
    ) -> str:
        t0 = time.perf_counter()
        op_status = "success"
        size_bytes = len(content) if content else 0
        with tracer.span("storage.s3.save", attributes={"storage.type": "s3"}):
            try:
                actual_hash = hashlib.sha256(content).hexdigest()
                if expected_hash and actual_hash != expected_hash:
                    op_status = "error"
                    raise StorageIntegrityError(
                        "SHA-256 mismatch before S3 upload: content does not match expected authoritative hash."
                    )

                effective_doc_id = document_id or actual_hash
                object_key = build_object_key(
                    tenant_id=tenant_id,
                    document_id=effective_doc_id,
                    version=version,
                    content_hash=actual_hash,
                )

                put_kwargs: Dict[str, Any] = {
                    "Bucket": self.bucket,
                    "Key": object_key,
                    "Body": content,
                    "Metadata": {
                        "sha256": actual_hash,
                        "tenant_id": tenant_id,
                        "version": str(version),
                    },
                }

                # SSE-KMS CMK enforcement
                if self.kms_key_id:
                    put_kwargs["ServerSideEncryption"] = "aws:kms"
                    put_kwargs["SSEKMSKeyId"] = self.kms_key_id
                else:
                    put_kwargs["ServerSideEncryption"] = "aws:kms"

                try:
                    self._client.put_object(**put_kwargs)
                except botocore.exceptions.ClientError as exc:
                    raise self._map_client_error(exc, object_key) from exc
                except Exception as exc:
                    raise StorageError(f"Failed to upload document to S3: {exc}") from exc

                return f"s3://{self.bucket}/{object_key}"
            except Exception:
                op_status = "error"
                raise
            finally:
                duration = time.perf_counter() - t0
                storage_operations_total.inc(1, {"storage_type": "s3", "operation": "save", "status": op_status})
                storage_duration_seconds.observe(duration, {"storage_type": "s3", "operation": "save"})
                if op_status == "success":
                    storage_bytes_total.inc(size_bytes, {"storage_type": "s3", "operation": "save"})

    def save_stream(
        self,
        stream: BinaryIO,
        filename: str,
        tenant_id: str,
        length: Optional[int] = None,
        document_id: Optional[str] = None,
        version: int = 1,
        expected_hash: Optional[str] = None,
    ) -> str:
        content = stream.read()
        return self.save(
            content=content,
            filename=filename,
            tenant_id=tenant_id,
            document_id=document_id,
            version=version,
            expected_hash=expected_hash,
        )

    def get(self, storage_path: str, expected_hash: Optional[str] = None) -> bytes:
        t0 = time.perf_counter()
        op_status = "success"
        body: Optional[bytes] = None
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        with tracer.span("storage.s3.get", attributes={"storage.type": "s3"}):
            try:
                try:
                    response = self._client.get_object(Bucket=bucket, Key=key)
                    body = response["Body"].read()
                except botocore.exceptions.ClientError as exc:
                    raise self._map_client_error(exc, key) from exc
                except Exception as exc:
                    raise StorageError(f"Failed to retrieve document from S3: {exc}") from exc

                # Authoritative SHA-256 checksum verification
                if expected_hash:
                    actual_hash = hashlib.sha256(body).hexdigest()
                    if actual_hash != expected_hash:
                        logger.error(
                            f"[S3DocumentStorage] Checksum mismatch on {key}: computed '{actual_hash}', authoritative '{expected_hash}'."
                        )
                        raise StorageIntegrityError(
                            "Integrity check failed: downloaded S3 content SHA-256 mismatch against authoritative PostgreSQL record."
                        )

                return body
            except Exception:
                op_status = "error"
                raise
            finally:
                duration = time.perf_counter() - t0
                storage_operations_total.inc(1, {"storage_type": "s3", "operation": "get", "status": op_status})
                storage_duration_seconds.observe(duration, {"storage_type": "s3", "operation": "get"})
                if op_status == "success" and body is not None:
                    storage_bytes_total.inc(len(body), {"storage_type": "s3", "operation": "get"})

    def download_verified(self, storage_path: str, expected_hash: str) -> bytes:
        if not expected_hash:
            raise StorageIntegrityError("download_verified requires non-empty expected_hash.")
        return self.get(storage_path, expected_hash=expected_hash)

    from contextlib import contextmanager

    @contextmanager
    def verified_temp_file(
        self,
        storage_path: str,
        expected_hash: str,
        suffix: str = ".tmp",
    ) -> Iterator[str]:
        import tempfile
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            streaming_body = response["Body"]
        except botocore.exceptions.ClientError as exc:
            raise self._map_client_error(exc, key) from exc
        except Exception as exc:
            raise StorageError(f"Failed to open S3 stream for temporary file download: {exc}") from exc

        temp_path = None
        hasher = hashlib.sha256()
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                temp_path = tf.name
                try:
                    while True:
                        chunk = streaming_body.read(65536)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        tf.write(chunk)
                finally:
                    streaming_body.close()

            actual_hash = hasher.hexdigest()
            if actual_hash != expected_hash:
                logger.error(
                    f"[S3DocumentStorage] Streaming tempfile checksum mismatch on {key}: "
                    f"computed '{actual_hash}', expected '{expected_hash}'."
                )
                raise StorageIntegrityError(
                    "Integrity check failed: downloaded S3 stream SHA-256 mismatch against authoritative PostgreSQL record."
                )

            yield temp_path
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def open_stream(
        self,
        storage_path: str,
        chunk_size: int = 65536,
        expected_hash: Optional[str] = None,
    ) -> Iterator[bytes]:
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            streaming_body = response["Body"]
        except botocore.exceptions.ClientError as exc:
            raise self._map_client_error(exc, key) from exc
        except Exception as exc:
            raise StorageError(f"Failed to open S3 stream: {exc}") from exc

        hasher = hashlib.sha256()
        try:
            while True:
                chunk = streaming_body.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
                yield chunk
        finally:
            streaming_body.close()

        if expected_hash:
            actual_hash = hasher.hexdigest()
            if actual_hash != expected_hash:
                raise StorageIntegrityError(
                    f"Streaming integrity check failed: computed '{actual_hash}' does not match authoritative '{expected_hash}'."
                )

    def delete(self, storage_path: str) -> bool:
        t0 = time.perf_counter()
        op_status = "success"
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        with tracer.span("storage.s3.delete", attributes={"storage.type": "s3"}):
            try:
                try:
                    self._client.delete_object(Bucket=bucket, Key=key)
                    return True
                except botocore.exceptions.ClientError as exc:
                    error_code = exc.response.get("Error", {}).get("Code", "")
                    if error_code in ("NoSuchKey", "404", "NotFound"):
                        return True  # Idempotent deletion
                    raise self._map_client_error(exc, key) from exc
                except Exception as exc:
                    raise StorageError(f"Failed to delete S3 object: {exc}") from exc
            except Exception:
                op_status = "error"
                raise
            finally:
                duration = time.perf_counter() - t0
                storage_operations_total.inc(1, {"storage_type": "s3", "operation": "delete", "status": op_status})
                storage_duration_seconds.observe(duration, {"storage_type": "s3", "operation": "delete"})

    def exists(self, storage_path: str) -> bool:
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404", "NotFound"):
                return False
            raise self._map_client_error(exc, key) from exc
        except Exception:
            return False

    def get_size(self, storage_path: str) -> int:
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        try:
            resp = self._client.head_object(Bucket=bucket, Key=key)
            return resp.get("ContentLength", 0)
        except Exception:
            return 0

    def get_metadata(self, storage_path: str) -> Dict[str, Any]:
        bucket, key = parse_s3_uri(storage_path, self.bucket)
        try:
            resp = self._client.head_object(Bucket=bucket, Key=key)
            return {
                "size": resp.get("ContentLength", 0),
                "etag": resp.get("ETag", "").strip('"'),
                "content_type": resp.get("ContentType"),
                "server_side_encryption": resp.get("ServerSideEncryption"),
                "sse_kms_key_id": resp.get("SSEKMSKeyId"),
                "metadata": resp.get("Metadata", {}),
                "last_modified": resp.get("LastModified"),
                "backend": "s3",
            }
        except botocore.exceptions.ClientError as exc:
            raise self._map_client_error(exc, key) from exc
        except Exception as exc:
            raise StorageError(f"Failed to get S3 metadata: {exc}") from exc

    def health_check(self) -> Dict[str, Any]:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return {
                "status": "HEALTHY",
                "backend": "s3",
                "bucket": self.bucket,
                "region": self.region,
                "kms_configured": bool(self.kms_key_id),
            }
        except botocore.exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            return {
                "status": "UNHEALTHY",
                "backend": "s3",
                "bucket": self.bucket,
                "error_code": error_code,
            }
        except Exception as exc:
            return {
                "status": "UNHEALTHY",
                "backend": "s3",
                "error": str(exc),
            }
