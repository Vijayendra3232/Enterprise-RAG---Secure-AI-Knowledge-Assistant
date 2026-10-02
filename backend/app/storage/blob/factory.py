"""
factory.py — Factory for instantiating and resolving DocumentStorageInterface providers.
Enforces fail-fast configuration validation for production environments.
"""

from typing import Optional
from app import config
from app.storage.blob.base import DocumentStorageInterface
from app.storage.blob.errors import StorageConfigurationError
from app.storage.blob.storage import LocalFilesystemStorage

_document_storage_instance: Optional[DocumentStorageInterface] = None


def get_document_storage() -> DocumentStorageInterface:
    """
    Resolve and return the configured DocumentStorageInterface singleton.
    In production environments, strictly enforces S3 configuration requirements.
    """
    global _document_storage_instance
    if _document_storage_instance is not None:
        return _document_storage_instance

    storage_type = (config.DOCUMENT_STORAGE_TYPE or "local").lower()

    if storage_type == "s3":
        if config.ENVIRONMENT == "production" and not config.S3_BUCKET:
            raise StorageConfigurationError(
                "CRITICAL SECURITY CONFIGURATION ERROR: S3_BUCKET must be configured for S3 document storage in production."
            )
        from app.storage.blob.s3 import S3DocumentStorage
        _document_storage_instance = S3DocumentStorage()
    else:
        if config.ENVIRONMENT == "production":
            # In production, warn or log if local filesystem is intentionally configured
            import logging
            logging.getLogger(__name__).warning(
                "[StorageFactory] LocalFilesystemStorage is active in production environment. Ensure this is intentional for single-node deployments."
            )
        _document_storage_instance = LocalFilesystemStorage()

    return _document_storage_instance


def reset_document_storage() -> None:
    """Reset the singleton instance for testing isolation."""
    global _document_storage_instance
    _document_storage_instance = None
