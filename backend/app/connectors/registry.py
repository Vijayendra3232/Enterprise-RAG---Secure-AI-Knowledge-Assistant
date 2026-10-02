"""
registry.py — Central registry for data source connector adapters.
Enables pluggable provider registration without modifying core synchronization logic.
"""

from typing import Dict, Type, Optional, Any
from app.connectors.base import DocumentConnector
from app.connectors.adapters.local import LocalConnector
from app.connectors.adapters.google_drive import GoogleDriveConnector
from app.connectors.adapters.microsoft_graph import MicrosoftGraphConnector
from app.connectors.errors import ConnectorError


class ConnectorRegistry:
    """
    Registry for data source connectors.
    """

    def __init__(self):
        self._registry: Dict[str, Type[DocumentConnector]] = {}
        # Automatically register verified adapters
        self.register("local", LocalConnector)
        self.register("google_drive", GoogleDriveConnector)
        self.register("microsoft_graph", MicrosoftGraphConnector)

    def register(self, connector_type: str, connector_cls: Type[DocumentConnector]) -> None:
        """Register a connector class for a specific provider type."""
        key = connector_type.lower().strip()
        self._registry[key] = connector_cls

    def get(self, connector_type: str) -> Optional[Type[DocumentConnector]]:
        """Retrieve connector class by type name."""
        return self._registry.get(connector_type.lower().strip())

    def create(
        self,
        connector_type: str,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any],
    ) -> DocumentConnector:
        """Instantiate a connector adapter instance."""
        cls = self.get(connector_type)
        if not cls:
            raise ConnectorError(
                f"Unsupported connector type: '{connector_type}'. "
                f"Supported types: {list(self._registry.keys())}"
            )
        return cls(tenant_id=tenant_id, connector_id=connector_id, config=config)

    def list_supported_types(self) -> list[str]:
        return list(self._registry.keys())


# Singleton instance
connector_registry = ConnectorRegistry()
