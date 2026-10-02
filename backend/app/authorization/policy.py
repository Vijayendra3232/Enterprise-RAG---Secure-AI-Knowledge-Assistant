"""
policy.py — Centralized authorization policy and permission enforcement.
"""

from typing import Set, Dict, Any, List, Optional
from fastapi import Depends, HTTPException, status

from app.authorization.roles import Role
from app.authorization.permissions import Permission
from app.auth.models import User
from app.security.audit import log_security_event


# Role-to-Permissions Mapping
ROLE_PERMISSIONS: Dict[str, Set[Permission]] = {
    Role.ADMIN.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
        Permission.USER_MANAGE,
        Permission.SETTINGS_MANAGE,
    },
    Role.MANAGER.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
    },
    Role.USER.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
    },
    Role.ENGINEERING.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
    },
    Role.FINANCE.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
    },
    Role.HR.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
    },
    Role.LEGAL.value: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_UPLOAD,
        Permission.DOCUMENT_DELETE,
    },
    Role.VIEWER.value: {
        Permission.DOCUMENT_READ,
    },
}


class AuthorizationPolicy:
    """
    Authoritative, deterministic policy evaluator for security decisions.
    """
    @staticmethod
    def get_permissions(role: str) -> List[Permission]:
        """Resolve all permissions associated with a role."""
        role_upper = role.upper()
        return list(ROLE_PERMISSIONS.get(role_upper, {Permission.DOCUMENT_READ}))

    @staticmethod
    def has_permission(role: str, permission: Permission) -> bool:
        """Check if a given role grants a specific permission."""
        role_upper = role.upper()
        allowed = ROLE_PERMISSIONS.get(role_upper, set())
        return permission in allowed

    @staticmethod
    def can(user: User, permission: Permission) -> bool:
        """Check if the user is authorized to perform an action."""
        if not user.is_active:
            return False
        return AuthorizationPolicy.has_permission(user.role, permission)

    @staticmethod
    def can_delete_document(user: User, doc_metadata: Dict[str, Any]) -> bool:
        """
        Check if user can delete a document.
        Enforces strict tenant isolation and ownership rules.
        """
        if not user.is_active or not AuthorizationPolicy.can(user, Permission.DOCUMENT_DELETE):
            return False

        doc_tenant = doc_metadata.get("tenant_id")
        # Strict tenant boundary: Company A users can never delete Company B documents
        if doc_tenant and doc_tenant != user.tenant_id:
            return False

        # Admins and Managers can delete any document within their tenant
        if user.role.upper() in {Role.ADMIN.value, Role.MANAGER.value}:
            return True

        # Standard users can only delete documents they own
        doc_owner = doc_metadata.get("owner_id") or doc_metadata.get("owner")
        return bool(doc_owner and doc_owner == user.user_id)


def require_permission(permission: Permission):
    """
    FastAPI dependency factory enforcing that the authenticated user possesses the required permission.
    """
    from app.auth.dependencies import get_current_user

    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not AuthorizationPolicy.can(current_user, permission):
            log_security_event(
                event_type="PERMISSION_DENIED",
                tenant_id=current_user.tenant_id,
                user_id=current_user.user_id,
                action=permission.value,
                result="denied",
                details={
                    "required_permission": permission.value,
                    "user_role": current_user.role
                }
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Missing required permission '{permission.value}'."
            )
        return current_user

    return permission_checker
