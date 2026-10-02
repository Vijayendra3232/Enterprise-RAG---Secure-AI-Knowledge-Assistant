"""
blob storage package.
"""

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

__all__ = [
    "DocumentStorageInterface",
    "LocalFilesystemStorage",
    "S3DocumentStorage",
    "build_object_key",
    "parse_s3_uri",
    "get_document_storage",
    "reset_document_storage",
    "StorageMigrationService",
    "MigrationState",
    "MigrationResult",
    "StorageError",
    "StorageNotFoundError",
    "StoragePermissionError",
    "StorageIntegrityError",
    "StorageConfigurationError",
]
