"""
models.py — Data models for sync planning, actions, execution metrics, and results.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

from app.connectors.base import ConnectorDocument


class SyncMode(str, Enum):
    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"


class SyncAction(str, Enum):
    IMPORT = "IMPORT"
    UPDATE = "UPDATE"
    UPDATE_PERMISSIONS = "UPDATE_PERMISSIONS"
    SKIP = "SKIP"
    DELETE = "DELETE"


class SyncPlannedItem(BaseModel):
    action: SyncAction
    source_id: str
    document_id: Optional[str] = None
    source_doc: Optional[ConnectorDocument] = None
    reason: str = ""
    model_config = {"arbitrary_types_allowed": True}


class SyncPlan(BaseModel):
    connector_id: str
    tenant_id: str
    sync_mode: SyncMode
    items: List[SyncPlannedItem] = Field(default_factory=list)

    @property
    def imports(self) -> List[SyncPlannedItem]:
        return [i for i in self.items if i.action == SyncAction.IMPORT]

    @property
    def updates(self) -> List[SyncPlannedItem]:
        return [i for i in self.items if i.action == SyncAction.UPDATE]

    @property
    def permission_updates(self) -> List[SyncPlannedItem]:
        return [i for i in self.items if i.action == SyncAction.UPDATE_PERMISSIONS]

    @property
    def skips(self) -> List[SyncPlannedItem]:
        return [i for i in self.items if i.action == SyncAction.SKIP]

    @property
    def deletions(self) -> List[SyncPlannedItem]:
        return [i for i in self.items if i.action == SyncAction.DELETE]


class SyncResult(BaseModel):
    run_id: str
    connector_id: str
    tenant_id: str
    sync_mode: SyncMode
    status: str = "SUCCESS"  # "SUCCESS", "ERROR", "PARTIAL"
    started_at: datetime
    completed_at: Optional[datetime] = None
    documents_seen: int = 0
    documents_added: int = 0
    documents_updated: int = 0
    documents_deleted: int = 0
    permissions_updated: int = 0
    documents_skipped: int = 0
    errors: List[Dict[str, Any]] = Field(default_factory=list)
