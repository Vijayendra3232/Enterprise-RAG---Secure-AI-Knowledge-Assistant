"""
base.py — Abstract DocumentStorageInterface for document binary blob storage.
Enforces explicit integrity contracts, streaming I/O vs verified state guarantees,
tenant isolation, and sanitized error reporting.
"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Dict, Any, Optional, BinaryIO, Iterator


class DocumentStorageInterface(ABC):
    """
    Abstract interface for physical document storage.
    Enforces tenant isolation, streaming I/O, integrity verification, and sanitized error reporting.

    INTEGRITY CONTRACT:
    - `open_stream()` provides raw chunked streaming. Chunks yielded in-flight are in the STREAMING
      state and must NOT be treated as trusted/verified until stream exhaustion confirms SHA-256 match.
    - `download_verified()` returns in-memory bytes strictly AFTER full SHA-256 verification (INTEGRITY_VERIFIED state).
    - `verified_temp_file()` streams chunks into a secure temporary filesystem path, calculates SHA-256
      incrementally, validates match against authoritative PostgreSQL content_hash, and yields the verified path.
      On mismatch, deletes the temp file immediately and raises StorageIntegrityError (fail closed).
    """

    @abstractmethod
    def save(
        self,
        content: bytes,
        filename: str,
        tenant_id: str,
        document_id: Optional[str] = None,
        version: int = 1,
        expected_hash: Optional[str] = None,
    ) -> str:
        """
        Save document binary content and return persistent storage path/URI.
        Enforces tenant scoping, deterministic object key derivation, and optional pre-upload hash verification.
        """
        pass

    @abstractmethod
    def get(self, storage_path: str, expected_hash: Optional[str] = None) -> bytes:
        """
        Retrieve document binary content by storage path/URI.
        If expected_hash is provided, verifies SHA-256 against PostgreSQL authoritative hash.
        Raises StorageIntegrityError on mismatch (fail closed).
        """
        pass

    @abstractmethod
    def download_verified(self, storage_path: str, expected_hash: str) -> bytes:
        """
        Explicit integrity-verified download contract.
        Guarantees content is verified against authoritative SHA-256 hash before returning.
        """
        pass

    @abstractmethod
    @contextmanager
    def verified_temp_file(
        self,
        storage_path: str,
        expected_hash: str,
        suffix: str = ".tmp",
    ) -> Iterator[str]:
        """
        Stream document chunks into a temporary file, calculate SHA-256 incrementally,
        and yield the temp file path only if integrity matches expected_hash.
        Guarantees deterministic cleanup of the temporary file in all exit paths.
        """
        pass

    @abstractmethod
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
        """
        Stream document content to storage without loading full payload into memory.
        """
        pass

    @abstractmethod
    def open_stream(
        self,
        storage_path: str,
        chunk_size: int = 65536,
        expected_hash: Optional[str] = None,
    ) -> Iterator[bytes]:
        """
        Stream document content from storage in chunks.
        Note: In-flight chunks are unverified until stream exhaustion if expected_hash is provided.
        Raises StorageIntegrityError upon stream exhaustion if hash does not match.
        """
        pass

    @abstractmethod
    def delete(self, storage_path: str) -> bool:
        """
        Delete document content by storage path/URI.
        Must be crash-safe and idempotent (returns True if file deleted or already nonexistent).
        """
        pass

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        """Check if document content exists at path/URI."""
        pass

    @abstractmethod
    def get_size(self, storage_path: str) -> int:
        """Get file size in bytes."""
        pass

    @abstractmethod
    def get_metadata(self, storage_path: str) -> Dict[str, Any]:
        """Retrieve storage metadata (e.g. content_type, etag, sha256, encryption)."""
        pass

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """Verify storage backend connectivity and readiness."""
        pass
