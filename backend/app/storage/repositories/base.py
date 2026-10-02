"""
base.py — Base repository interfaces and exceptions.
"""

from abc import ABC


class StorageException(Exception):
    """Base exception for storage and repository operations."""
    pass


class ConcurrencyConflictException(StorageException):
    """Raised when an optimistic concurrency version check fails."""
    pass


class EntityNotFoundException(StorageException):
    """Raised when a requested entity does not exist."""
    pass


class DuplicateEntityException(StorageException):
    """Raised when a unique constraint or composite key is violated."""
    pass
