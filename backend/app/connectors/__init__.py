"""
connectors package — Data source connectors, authoritative ACL normalization, and synchronization.
"""

from app.connectors.base import (
    DocumentConnector,
    ConnectorDocument,
    ConnectorACL,
    ConnectorPermission,
    Principal,
    PrincipalType,
    PermissionEffect,
)
from app.connectors.errors import (
    ConnectorError,
    AuthenticationError,
    SourceUnavailableError,
    RateLimitError,
    PermissionSyncError,
    MalformedDocumentError,
    ConnectorNotFoundError,
)
from app.connectors.registry import connector_registry
from app.connectors.permissions import PermissionNormalizer, NormalizedPermissionSet

__all__ = [
    "DocumentConnector",
    "ConnectorDocument",
    "ConnectorACL",
    "ConnectorPermission",
    "Principal",
    "PrincipalType",
    "PermissionEffect",
    "ConnectorError",
    "AuthenticationError",
    "SourceUnavailableError",
    "RateLimitError",
    "PermissionSyncError",
    "MalformedDocumentError",
    "ConnectorNotFoundError",
    "connector_registry",
    "PermissionNormalizer",
    "NormalizedPermissionSet",
]
