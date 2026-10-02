from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.authorization.context import AuthorizationContext
from app.authorization.filters import is_document_accessible


class MetadataFilter(BaseModel):
    """
    Standard filters that can be applied to metadata before/during retrieval.
    Enforces deterministic security constraints via AuthorizationContext.
    """
    document_id: Optional[str] = None
    filename: Optional[str] = None
    file_type: Optional[str] = None
    source: Optional[str] = None
    page: Optional[int] = None
    document_version: Optional[str] = None

    # Access control & tenancy fields
    tenant_id: Optional[str] = None
    owner_id: Optional[str] = None
    owner: Optional[str] = None
    access_level: Optional[str] = None
    allowed_roles: Optional[List[str]] = None
    allowed_user_ids: Optional[List[str]] = None

    # Immutable authorization context
    auth_context: Optional[AuthorizationContext] = None

    def to_chroma_filter(self) -> Optional[Dict[str, Any]]:
        """
        Convert active filters into Chroma's query where syntax.
        Combines user metadata filters with strict tenant isolation.
        """
        active_filters = []

        # 1. User metadata filters
        if self.document_id is not None:
            active_filters.append({"document_id": self.document_id})
        if self.filename is not None:
            active_filters.append({"filename": self.filename})
        if self.file_type is not None:
            active_filters.append({"file_type": self.file_type})
        if self.source is not None:
            active_filters.append({"source": self.source})
        if self.page is not None:
            active_filters.append({"page": self.page})
        if self.document_version is not None:
            active_filters.append({"document_version": self.document_version})

        # 2. Explicit tenant / owner filters
        if self.tenant_id is not None:
            active_filters.append({"tenant_id": self.tenant_id})
        elif self.auth_context is not None and self.auth_context.tenant_id:
            # Enforce tenant isolation in vector search if tenant_id is specified
            active_filters.append({"tenant_id": self.auth_context.tenant_id})

        if self.owner_id is not None:
            active_filters.append({"owner_id": self.owner_id})
        elif self.owner is not None:
            active_filters.append({"owner_id": self.owner})

        if not active_filters:
            return None

        if len(active_filters) == 1:
            return active_filters[0]

        return {"$and": active_filters}

    def matches(self, metadata: Dict[str, Any]) -> bool:
        """
        Evaluate if a document metadata dictionary matches both the active filters
        and the authorization policy.
        Used for BM25 and pre/post retrieval authorization filtering.
        """
        # ── 1. Authoritative security check ──────────────────────────────────
        if self.auth_context is not None:
            if not is_document_accessible(metadata, self.auth_context):
                return False

        # ── 2. Standard metadata filters ─────────────────────────────────────
        if self.document_id is not None and metadata.get("document_id") != self.document_id:
            return False
        if self.filename is not None and metadata.get("filename") != self.filename:
            return False
        if self.file_type is not None and metadata.get("file_type") != self.file_type:
            return False
        if self.source is not None and metadata.get("source") != self.source:
            return False
        if self.page is not None and metadata.get("page") != self.page:
            return False
        if self.document_version is not None and metadata.get("document_version") != self.document_version:
            return False

        # Tenancy & Ownership filters
        if self.tenant_id is not None and metadata.get("tenant_id") != self.tenant_id:
            return False
        doc_owner = metadata.get("owner_id") or metadata.get("owner")
        if self.owner_id is not None and doc_owner != self.owner_id:
            return False
        if self.owner is not None and doc_owner != self.owner:
            return False

        return True
