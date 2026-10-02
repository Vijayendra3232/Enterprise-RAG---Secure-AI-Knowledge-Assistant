"""
errors.py — Exception hierarchy for Document Storage subsystem.
Ensures sanitized error messages without credential or internal path leakage.
"""


class StorageError(Exception):
    """Base exception for all document storage operations."""
    pass


class StorageNotFoundError(StorageError):
    """Raised when the requested storage object or path does not exist."""
    pass


class StoragePermissionError(StorageError):
    """Raised when access to the storage resource is denied."""
    pass


class StorageIntegrityError(StorageError):
    """Raised when document content fails SHA-256 checksum integrity verification."""
    pass


class StorageConfigurationError(StorageError):
    """Raised when storage configuration is invalid or missing in production."""
    pass
