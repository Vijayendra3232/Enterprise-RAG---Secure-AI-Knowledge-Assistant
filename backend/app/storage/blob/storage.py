"""
storage.py — LocalFilesystemStorage implementation [DEV/TEST ONLY].
Provides local filesystem storage with tenant folder isolation and integrity verification.
"""

import hashlib
import io
import os
import time
from typing import Optional, Dict, Any, BinaryIO, Iterator

from app import config
from app.storage.blob.base import DocumentStorageInterface
from app.storage.blob.errors import (
    StorageNotFoundError,
    StorageIntegrityError,
    StoragePermissionError,
)
from app.observability.metrics import (
    storage_operations_total,
    storage_duration_seconds,
    storage_bytes_total,
)
from app.observability.tracing import tracer


class LocalFilesystemStorage(DocumentStorageInterface):
    """
    [DEV/TEST ONLY] Local filesystem storage implementation with tenant-level folder isolation.
    Not intended as primary cloud storage in multi-node AWS ECS/Fargate production deployments.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = os.path.abspath(base_dir or config.UPLOAD_DIR)
        os.makedirs(self.base_dir, exist_ok=True)

    def _get_tenant_dir(self, tenant_id: str) -> str:
        # Sanitize tenant_id to prevent directory traversal
        safe_tenant = "".join(c for c in tenant_id if c.isalnum() or c in ("-", "_")).strip()
        if not safe_tenant:
            safe_tenant = "default"
        tenant_path = os.path.join(self.base_dir, safe_tenant)
        os.makedirs(tenant_path, exist_ok=True)
        return tenant_path

    def _safe_filename(
        self,
        filename: str,
        content_hash: str,
        document_id: Optional[str] = None,
        version: int = 1,
    ) -> str:
        stem, ext = (
            filename.rsplit(".", 1)
            if "." in filename
            else (filename, "")
        )
        safe_stem = "".join(c for c in stem if c.isalnum() or c in ("-", "_", " ")).strip() or "document"
        if document_id:
            safe_doc_id = "".join(c for c in document_id if c.isalnum() or c in ("-", "_")).strip()
            return f"{safe_doc_id}_v{version}_{content_hash[:8]}.{ext}" if ext else f"{safe_doc_id}_v{version}_{content_hash[:8]}"
        return f"{safe_stem}_{content_hash[:8]}.{ext}" if ext else f"{safe_stem}_{content_hash[:8]}"

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
        with tracer.span("storage.save", attributes={"storage.type": "local"}):
            try:
                actual_hash = hashlib.sha256(content).hexdigest()
                if expected_hash and actual_hash != expected_hash:
                    op_status = "error"
                    raise StorageIntegrityError(
                        "SHA-256 mismatch during local storage save: content does not match expected authoritative hash."
                    )

                tenant_dir = self._get_tenant_dir(tenant_id)
                safe_name = self._safe_filename(filename, actual_hash, document_id, version)
                target_path = os.path.join(tenant_dir, safe_name)

                with open(target_path, "wb") as fh:
                    fh.write(content)
                return target_path
            except Exception:
                op_status = "error"
                raise
            finally:
                duration = time.perf_counter() - t0
                storage_operations_total.inc(1, {"storage_type": "local", "operation": "save", "status": op_status})
                storage_duration_seconds.observe(duration, {"storage_type": "local", "operation": "save"})
                if op_status == "success":
                    storage_bytes_total.inc(size_bytes, {"storage_type": "local", "operation": "save"})

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
        content: Optional[bytes] = None
        with tracer.span("storage.get", attributes={"storage.type": "local"}):
            try:
                if not self.exists(storage_path):
                    op_status = "error"
                    raise StorageNotFoundError(f"Storage path '{storage_path}' not found.")
                try:
                    with open(storage_path, "rb") as fh:
                        content = fh.read()
                except PermissionError as exc:
                    op_status = "error"
                    raise StoragePermissionError(f"Permission denied accessing storage path: {exc}") from exc

                if expected_hash:
                    actual_hash = hashlib.sha256(content).hexdigest()
                    if actual_hash != expected_hash:
                        op_status = "error"
                        raise StorageIntegrityError(
                            f"Integrity check failed: local file hash '{actual_hash}' does not match authoritative hash '{expected_hash}'."
                        )

                return content
            except Exception:
                op_status = "error"
                raise
            finally:
                duration = time.perf_counter() - t0
                storage_operations_total.inc(1, {"storage_type": "local", "operation": "get", "status": op_status})
                storage_duration_seconds.observe(duration, {"storage_type": "local", "operation": "get"})
                if op_status == "success" and content is not None:
                    storage_bytes_total.inc(len(content), {"storage_type": "local", "operation": "get"})

    def download_verified(self, storage_path: str, expected_hash: str) -> bytes:
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
        if not self.exists(storage_path):
            raise StorageNotFoundError(f"Storage path '{storage_path}' not found.")

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                temp_path = tf.name
                hasher = hashlib.sha256()
                with open(storage_path, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        tf.write(chunk)

            actual_hash = hasher.hexdigest()
            if actual_hash != expected_hash:
                raise StorageIntegrityError(
                    f"Integrity check failed: temporary file hash '{actual_hash}' does not match authoritative hash '{expected_hash}'."
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
        if not self.exists(storage_path):
            raise StorageNotFoundError(f"Storage path '{storage_path}' not found.")
        
        hasher = hashlib.sha256()
        with open(storage_path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
                yield chunk

        if expected_hash:
            actual_hash = hasher.hexdigest()
            if actual_hash != expected_hash:
                raise StorageIntegrityError(
                    f"Streaming integrity check failed: computed hash '{actual_hash}' does not match authoritative hash '{expected_hash}'."
                )

    def delete(self, storage_path: str) -> bool:
        t0 = time.perf_counter()
        op_status = "success"
        with tracer.span("storage.delete", attributes={"storage.type": "local"}):
            try:
                if not self.exists(storage_path):
                    return False
                try:
                    os.remove(storage_path)
                    return True
                except OSError:
                    op_status = "error"
                    return False
            finally:
                duration = time.perf_counter() - t0
                storage_operations_total.inc(1, {"storage_type": "local", "operation": "delete", "status": op_status})
                storage_duration_seconds.observe(duration, {"storage_type": "local", "operation": "delete"})

    def exists(self, storage_path: str) -> bool:
        return bool(storage_path and os.path.exists(storage_path) and os.path.isfile(storage_path))

    def get_size(self, storage_path: str) -> int:
        if not self.exists(storage_path):
            return 0
        return os.path.getsize(storage_path)

    def get_metadata(self, storage_path: str) -> Dict[str, Any]:
        if not self.exists(storage_path):
            raise StorageNotFoundError(f"Storage path '{storage_path}' not found.")
        stat = os.stat(storage_path)
        return {
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
            "backend": "local_filesystem",
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            is_writable = os.access(self.base_dir, os.W_OK)
            return {
                "status": "HEALTHY" if is_writable else "DEGRADED",
                "backend": "local_filesystem",
                "base_dir": self.base_dir,
                "writable": is_writable,
            }
        except Exception as exc:
            return {
                "status": "UNHEALTHY",
                "backend": "local_filesystem",
                "error": str(exc),
            }
