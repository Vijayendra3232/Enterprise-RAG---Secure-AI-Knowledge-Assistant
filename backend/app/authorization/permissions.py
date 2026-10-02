"""
permissions.py — Centralized permission definitions.
"""

from enum import Enum


class Permission(str, Enum):
    """
    Granular permission definitions.
    """
    DOCUMENT_READ = "DOCUMENT_READ"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    DOCUMENT_DELETE = "DOCUMENT_DELETE"
    USER_MANAGE = "USER_MANAGE"
    SETTINGS_MANAGE = "SETTINGS_MANAGE"
