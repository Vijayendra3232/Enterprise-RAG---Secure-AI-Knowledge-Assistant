"""
context.py — AuthorizationContext model for conveying security identity into services.
"""

from typing import List, Optional
from pydantic import BaseModel, Field
from app.authorization.permissions import Permission


class AuthorizationContext(BaseModel):
    """
    Immutable security context created from verified authentication tokens.
    Passed through RAG pipelines to enforce multi-tenant and document-level access control.
    """
    user_id: str
    tenant_id: str
    role: str
    permissions: List[Permission] = Field(default_factory=list)
    groups: List[str] = Field(default_factory=list)
    
    # Optional dynamic/delegated ACL constraints
    allowed_document_ids: Optional[List[str]] = None
    allowed_access_levels: Optional[List[str]] = None
    allowed_roles: Optional[List[str]] = None
