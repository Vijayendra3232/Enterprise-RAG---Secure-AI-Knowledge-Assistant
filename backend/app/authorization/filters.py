"""
filters.py — Deterministic document access control evaluation.
"""

from typing import Dict, Any, Optional, List
from app import config
from app.authorization.roles import Role
from app.authorization.context import AuthorizationContext


def is_document_accessible(
    doc_metadata: Dict[str, Any],
    auth_context: Optional[AuthorizationContext]
) -> bool:
    """
    Deterministic document accessibility evaluator.
    Enforces multi-tenant boundaries, user ACLs, role ACLs, and fail-safe defaults.
    """
    from app.observability.metrics import (
        authz_checks_total,
        authz_allow_total,
        authz_denied_total,
        cross_tenant_attempts_total,
        unknown_permissions_total,
    )
    from app.observability.events import emit_security_event

    access_level = (doc_metadata.get("access_level") or "UNKNOWN").upper()
    authz_checks_total.inc(labels={"access_level": access_level})

    # If auth context is not provided, allow in dev mode for backward compatibility
    if auth_context is None:
        if config.ENVIRONMENT == "development":
            authz_allow_total.inc(labels={"access_level": access_level})
            return True
        authz_denied_total.inc(labels={"reason": "missing_auth_context"})
        return False

    doc_tenant = doc_metadata.get("tenant_id")
    # ── 1. Tenant Isolation ───────────────────────────────────────────────────
    # Documents belonging to another tenant can NEVER be accessed
    if doc_tenant and doc_tenant != auth_context.tenant_id:
        cross_tenant_attempts_total.inc(labels={"resource_type": "document"})
        authz_denied_total.inc(labels={"reason": "cross_tenant"})
        emit_security_event(
            "CROSS_TENANT_ATTEMPT",
            action="access_document",
            result="denied",
            tenant_id=auth_context.tenant_id,
            user_id=auth_context.user_id,
            details={"doc_tenant": doc_tenant},
        )
        return False

    # If document has no tenant_id metadata (e.g. unannotated legacy dev corpus)
    if not doc_tenant:
        if config.ENVIRONMENT == "development":
            pass
        else:
            authz_denied_total.inc(labels={"reason": "missing_tenant_metadata"})
            return False

    # ── 2. Explicit DENY Precedence ──────────────────────────────────────────
    # Check explicit deny lists for user, role, and groups
    raw_denied_users = doc_metadata.get("denied_user_ids") or []
    denied_user_ids = [str(u) for u in raw_denied_users] if not isinstance(raw_denied_users, str) else [raw_denied_users]
    if auth_context.user_id in denied_user_ids:
        authz_denied_total.inc(labels={"reason": "explicit_deny_user"})
        return False

    raw_denied_roles = doc_metadata.get("denied_roles") or []
    denied_roles = [str(r).upper() for r in raw_denied_roles] if not isinstance(raw_denied_roles, str) else [raw_denied_roles.upper()]
    if auth_context.role.upper() in denied_roles:
        authz_denied_total.inc(labels={"reason": "explicit_deny_role"})
        return False

    raw_denied_groups = doc_metadata.get("denied_groups") or []
    denied_groups = [str(g).lower() for g in raw_denied_groups] if not isinstance(raw_denied_groups, str) else [raw_denied_groups.lower()]
    user_groups = [str(g).lower() for g in getattr(auth_context, "groups", []) or []]
    if any(g in denied_groups for g in user_groups):
        authz_denied_total.inc(labels={"reason": "explicit_deny_group"})
        return False

    # ── 3. Admin within Tenant ───────────────────────────────────────────────
    # Administrators can read all documents belonging to their own tenant (unless explicitly denied above)
    if auth_context.role.upper() == Role.ADMIN.value:
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # ── 4. Access Level, Groups, and ACLs ────────────────────────────────────
    owner_id = doc_metadata.get("owner_id") or doc_metadata.get("owner")

    permission_status = (doc_metadata.get("permission_status") or "KNOWN").upper()
    # If permission status is UNKNOWN, apply fail-closed fallback (owner only)
    if permission_status == "UNKNOWN":
        is_owner = bool(owner_id and owner_id == auth_context.user_id)
        unknown_permissions_total.inc(labels={"decision": "allow" if is_owner else "deny"})
        if not is_owner:
            authz_denied_total.inc(labels={"reason": "unknown_permission_fail_closed"})
            emit_security_event(
                "UNKNOWN_PERMISSION_FAIL_CLOSED",
                action="evaluate_permission",
                result="denied",
                tenant_id=auth_context.tenant_id,
                user_id=auth_context.user_id,
            )
            return False
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    raw_allowed_roles = doc_metadata.get("allowed_roles") or []
    allowed_roles = [str(r).upper() for r in raw_allowed_roles] if not isinstance(raw_allowed_roles, str) else [raw_allowed_roles.upper()]

    raw_allowed_users = doc_metadata.get("allowed_user_ids") or []
    allowed_user_ids = [str(u) for u in raw_allowed_users] if not isinstance(raw_allowed_users, str) else [raw_allowed_users]

    raw_allowed_groups = doc_metadata.get("allowed_groups") or []
    allowed_groups = [str(g).lower() for g in raw_allowed_groups] if not isinstance(raw_allowed_groups, str) else [raw_allowed_groups.lower()]

    # Direct User match
    if auth_context.user_id in allowed_user_ids:
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # Group match
    if any(g in allowed_groups for g in user_groups):
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # Role match
    if auth_context.role.upper() in allowed_roles:
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # Public or Tenant-wide access
    if access_level in {"PUBLIC", "TENANT"}:
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # Role-based access level
    if access_level == "ROLE":
        if auth_context.role.upper() in allowed_roles or (owner_id and owner_id == auth_context.user_id):
            authz_allow_total.inc(labels={"access_level": access_level})
            return True
        authz_denied_total.inc(labels={"reason": "role_mismatch"})
        return False

    # Group-based access level
    if access_level == "GROUP":
        if any(g in allowed_groups for g in user_groups) or (owner_id and owner_id == auth_context.user_id):
            authz_allow_total.inc(labels={"access_level": access_level})
            return True
        authz_denied_total.inc(labels={"reason": "group_mismatch"})
        return False

    # User-specific access level
    if access_level == "USER":
        if auth_context.user_id in allowed_user_ids or (owner_id and owner_id == auth_context.user_id):
            authz_allow_total.inc(labels={"access_level": access_level})
            return True
        authz_denied_total.inc(labels={"reason": "user_mismatch"})
        return False

    # Private document (owner only)
    if access_level == "PRIVATE":
        if owner_id and owner_id == auth_context.user_id:
            authz_allow_total.inc(labels={"access_level": access_level})
            return True
        authz_denied_total.inc(labels={"reason": "private_not_owner"})
        return False

    if owner_id and owner_id == auth_context.user_id:
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # If document has no access metadata at all:
    if config.ENVIRONMENT == "development":
        authz_allow_total.inc(labels={"access_level": access_level})
        return True

    # Production default: fail closed (unannotated documents are private)
    authz_denied_total.inc(labels={"reason": "default_fail_closed"})
    return False
