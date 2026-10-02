"""
planner.py — Deterministic sync planner comparing external source documents against PostgreSQL.
Generates minimal SyncPlan categorizing items into IMPORT, UPDATE, UPDATE_PERMISSIONS, SKIP, and DELETE.
"""

from typing import List, Dict, Optional, Set
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorDocument
from app.connectors.sync.models import (
    SyncMode,
    SyncAction,
    SyncPlan,
    SyncPlannedItem,
)
from app.storage.models.document import Document as DBDocument


class SyncPlanner:
    """
    Compares external source state against PostgreSQL authoritative records
    and builds an execution plan.
    """

    @staticmethod
    def plan(
        connector_id: str,
        tenant_id: str,
        source_type: str,
        sync_mode: SyncMode,
        discovered_docs: List[ConnectorDocument],
        existing_docs: List[DBDocument],
        is_complete_discovery: bool = True,
        deleted_source_ids: Optional[List[str]] = None,
    ) -> SyncPlan:
        """
        Build deterministic SyncPlan.

        Decision Rules:
        1. Not in DB -> IMPORT
        2. In DB, content_hash changed -> UPDATE
        3. In DB, content_hash identical, ACL changed -> UPDATE_PERMISSIONS
        4. In DB, content_hash identical, ACL identical -> SKIP
        5. Incremental sync with explicit deleted_source_ids -> DELETE
        6. Full sync & missing in external source & complete discovery -> DELETE
        """
        items: List[SyncPlannedItem] = []
        existing_map: Dict[str, DBDocument] = {}

        for doc in existing_docs:
            src_doc_id = doc.source_document_id
            if src_doc_id:
                existing_map[src_doc_id] = doc

        discovered_source_ids: Set[str] = set()

        for s_doc in discovered_docs:
            src_id = s_doc.source_id
            discovered_source_ids.add(src_id)
            existing = existing_map.get(src_id)

            if existing is None:
                # 1. New document
                items.append(
                    SyncPlannedItem(
                        action=SyncAction.IMPORT,
                        source_id=src_id,
                        document_id=None,
                        source_doc=s_doc,
                        reason="New document discovered in source",
                    )
                )
            else:
                # Existing document
                doc_id = existing.document_id
                if existing.content_hash != s_doc.content_hash or existing.status != "INDEXED":
                    # 2. Content modified or previously failed
                    items.append(
                        SyncPlannedItem(
                            action=SyncAction.UPDATE,
                            source_id=src_id,
                            document_id=doc_id,
                            source_doc=s_doc,
                            reason="Document content modified or requires re-indexing",
                        )
                    )
                else:
                    # Content is identical; check if ACL changed
                    old_acl_hash = (existing.metadata_json or {}).get("acl_hash")
                    new_acl_hash = s_doc.acl.canonical_hash() if s_doc.acl else "UNKNOWN"

                    if old_acl_hash != new_acl_hash:
                        # 3. Permission-only update
                        items.append(
                            SyncPlannedItem(
                                action=SyncAction.UPDATE_PERMISSIONS,
                                source_id=src_id,
                                document_id=doc_id,
                                source_doc=s_doc,
                                reason="Document content unchanged; ACL modified",
                            )
                        )
                    else:
                        # 4. Unchanged document -> Skip
                        items.append(
                            SyncPlannedItem(
                                action=SyncAction.SKIP,
                                source_id=src_id,
                                document_id=doc_id,
                                source_doc=s_doc,
                                reason="Document content and ACL identical",
                            )
                        )

        # 5. Incremental Change Feed Explicit Deletions
        if deleted_source_ids:
            for del_src_id in deleted_source_ids:
                existing = existing_map.get(del_src_id)
                if existing:
                    items.append(
                        SyncPlannedItem(
                            action=SyncAction.DELETE,
                            source_id=del_src_id,
                            document_id=existing.document_id,
                            source_doc=None,
                            reason="Explicitly marked removed in incremental change stream",
                        )
                    )

        # 6. Full Sync Deletion Detection
        # CRITICAL SAFETY INVARIANT: If discovery is incomplete, truncated, or errored, NEVER plan deletions!
        if sync_mode == SyncMode.FULL and is_complete_discovery:
            for src_id, existing in existing_map.items():
                if src_id not in discovered_source_ids:
                    items.append(
                        SyncPlannedItem(
                            action=SyncAction.DELETE,
                            source_id=src_id,
                            document_id=existing.document_id,
                            source_doc=None,
                            reason="Document removed from external source",
                        )
                    )

        return SyncPlan(
            connector_id=connector_id,
            tenant_id=tenant_id,
            sync_mode=sync_mode,
            items=items,
        )
