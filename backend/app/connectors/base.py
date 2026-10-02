"""
base.py — Generic, provider-independent Cloud Connector interface and normalized models.
Defines contracts for discovering, extracting, and normalizing documents and authoritative ACLs.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class PrincipalType(str, Enum):
    USER = "USER"
    GROUP = "GROUP"
    ROLE = "ROLE"


class PermissionEffect(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class Principal(BaseModel):
    principal_type: PrincipalType
    principal_id: str
    display_name: Optional[str] = None
    model_config = {"frozen": True}


class ConnectorPermission(BaseModel):
    principal: Principal
    permission: str = "DOCUMENT_READ"
    effect: PermissionEffect = PermissionEffect.ALLOW
    model_config = {"frozen": True}


class ConnectorACL(BaseModel):
    permissions: List[ConnectorPermission] = Field(default_factory=list)
    permission_status: str = "KNOWN"  # "KNOWN" or "UNKNOWN"

    def canonical_hash(self) -> str:
        """
        Generate a deterministic, stable SHA-256 hash representing this ACL
        regardless of JSON key or list ordering.
        """
        import hashlib
        import json

        sorted_perms = sorted(
            [
                {
                    "principal_type": p.principal.principal_type.value,
                    "principal_id": p.principal.principal_id.strip().upper(),
                    "permission": p.permission.strip().upper(),
                    "effect": p.effect.value,
                }
                for p in self.permissions
            ],
            key=lambda x: (x["principal_type"], x["principal_id"], x["permission"], x["effect"]),
        )
        canonical_payload = {
            "permission_status": self.permission_status.upper(),
            "permissions": sorted_perms,
        }
        serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ConnectorDocument(BaseModel):
    """
    Normalized document extracted by a connector from an external source.
    """
    source_type: str
    source_id: str
    source_path: Optional[str] = None
    source_url: Optional[str] = None
    name: str
    mime_type: Optional[str] = None
    size_bytes: int = 0
    content: bytes = Field(repr=False)
    content_hash: str
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    source_version: Optional[str] = None
    source_parent_id: Optional[str] = None
    source_modified_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    acl: Optional[ConnectorACL] = None


class DocumentConnector(ABC):
    """
    Abstract Base Class for all external data source connectors.
    Provides standard lifecycle operations for discovery, document extraction,
    and change tracking.
    """

    def __init__(self, tenant_id: str, connector_id: str, config: Dict[str, Any]):
        self.tenant_id = tenant_id
        self.connector_id = connector_id
        self.config = config

    @abstractmethod
    def validate_configuration(self) -> bool:
        """Verify credentials and source accessibility."""
        pass

    @abstractmethod
    def list_documents(self) -> List[ConnectorDocument]:
        """Discover and list all documents available at the source."""
        pass

    @abstractmethod
    def get_document(self, source_id: str) -> Optional[ConnectorDocument]:
        """Retrieve a specific document by its provider source ID."""
        pass

    @abstractmethod
    def get_permissions(self, source_id: str) -> Optional[ConnectorACL]:
        """Fetch the authoritative ACL for a specific document."""
        pass

    def get_changes(self, since_timestamp: Optional[datetime] = None) -> List[ConnectorDocument]:
        """
        Fetch incremental changes since the provided timestamp.
        If unsupported by the provider, fallback to full listing.
        """
        return self.list_documents()

    def fetch_changes(self, cursor: Optional[str] = None) -> tuple[List[ConnectorDocument], List[str], Optional[str]]:
        """
        Fetch incremental changes using a provider change token / delta cursor.
        Returns:
            Tuple of (changed_documents, deleted_source_ids, next_cursor).
        """
        docs = self.get_changes()
        return docs, [], None

    def health_check(self) -> Dict[str, Any]:
        """
        Verify provider connectivity and credentials without leaking tokens.
        """
        is_valid = self.validate_configuration()
        return {
            "status": "HEALTHY" if is_valid else "UNHEALTHY",
            "connector_id": self.connector_id,
            "tenant_id": self.tenant_id,
        }

    def refresh_credentials(self) -> Optional[Dict[str, Any]]:
        """
        Perform thread-safe credential/token refresh if needed and return updated configuration dictionary.
        Returns None if credentials were not changed.
        """
        return None
