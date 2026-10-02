"""
permissions.py — Permission normalizer converting external connector ACLs into internal authorization models.
Supports deterministic parsing of USER, GROUP, and ROLE principals with explicit ALLOW and DENY semantics.
"""

from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

from app.connectors.base import (
    ConnectorACL,
    ConnectorPermission,
    PrincipalType,
    PermissionEffect,
)


class NormalizedPermissionSet(BaseModel):
    """
    Standardized authorization representation ready for persistence and query filtering.
    """
    access_level: str = "PRIVATE"
    permission_status: str = "KNOWN"
    allowed_roles: List[str] = Field(default_factory=list)
    allowed_user_ids: List[str] = Field(default_factory=list)
    allowed_groups: List[str] = Field(default_factory=list)
    denied_roles: List[str] = Field(default_factory=list)
    denied_user_ids: List[str] = Field(default_factory=list)
    denied_groups: List[str] = Field(default_factory=list)
    db_permission_records: List[Dict[str, Any]] = Field(default_factory=list)


class PermissionNormalizer:
    """
    Deterministic converter transforming provider-specific ACLs into internal security metadata.
    """

    @staticmethod
    def normalize(
        acl: Optional[ConnectorACL],
        tenant_id: str,
        document_id: str,
        owner_id: Optional[str] = None,
        connector_config: Optional[Dict[str, Any]] = None,
    ) -> NormalizedPermissionSet:
        """
        Normalize an external ConnectorACL.

        Rules:
        1. If ACL is None or marked UNKNOWN, fail-closed with UNKNOWN status.
        2. 'anyone' / public links fail closed as UNKNOWN unless connector_config explicitly has allow_public_link_access=True.
        3. 'domain' permissions only grant access if the domain matches an entry in allowed_domains with allow_domain_access=True.
        4. Categorize principals into allowed/denied for USER, GROUP, and ROLE with explicit DENY precedence.
        5. Determine appropriate access_level (PUBLIC, ROLE, GROUP, USER, PRIVATE).
        6. Generate DB permission record dictionaries.
        """
        cfg = connector_config or {}
        allow_public_links = bool(cfg.get("allow_public_link_access", False))
        allow_domain_access = bool(cfg.get("allow_domain_access", False))
        allowed_domains = [d.lower().strip() for d in cfg.get("allowed_domains", [])]

        if acl is None or acl.permission_status.upper() == "UNKNOWN":
            return NormalizedPermissionSet(
                access_level="PRIVATE",
                permission_status="UNKNOWN",
                allowed_roles=[],
                allowed_user_ids=[owner_id] if owner_id else [],
                allowed_groups=[],
                denied_roles=[],
                denied_user_ids=[],
                denied_groups=[],
                db_permission_records=[],
            )

        allowed_roles: List[str] = []
        allowed_user_ids: List[str] = []
        allowed_groups: List[str] = []
        denied_roles: List[str] = []
        denied_user_ids: List[str] = []
        denied_groups: List[str] = []
        db_records: List[Dict[str, Any]] = []
        has_unknown_or_unmapped = False

        for perm in acl.permissions:
            p_type = perm.principal.principal_type
            p_id = perm.principal.principal_id.strip()
            effect = perm.effect

            record = {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "permission": perm.permission or "DOCUMENT_READ",
                "effect": effect.value,
                "role": None,
                "user_id": None,
                "group_id": None,
            }

            if p_type == PrincipalType.ROLE:
                role_val = p_id.upper()
                if role_val in ("PUBLIC", "ANYONE", "*"):
                    if allow_public_links:
                        role_val = "PUBLIC"
                        record["role"] = "PUBLIC"
                        if effect == PermissionEffect.ALLOW:
                            if "PUBLIC" not in allowed_roles:
                                allowed_roles.append("PUBLIC")
                        else:
                            if "PUBLIC" not in denied_roles:
                                denied_roles.append("PUBLIC")
                        db_records.append(record)
                    else:
                        # Fail-closed: Google 'anyone' is rejected by default
                        has_unknown_or_unmapped = True

                elif role_val.startswith("DOMAIN:"):
                    domain_name = p_id.split(":", 1)[1].lower().strip()
                    if allow_domain_access and (not allowed_domains or domain_name in allowed_domains):
                        # Domain verified
                        domain_role = f"DOMAIN_{domain_name.upper().replace('.', '_')}"
                        record["role"] = domain_role
                        if effect == PermissionEffect.ALLOW:
                            if domain_role not in allowed_roles:
                                allowed_roles.append(domain_role)
                        else:
                            if domain_role not in denied_roles:
                                denied_roles.append(domain_role)
                        db_records.append(record)
                    else:
                        # Unverified external domain fails closed
                        has_unknown_or_unmapped = True
                else:
                    record["role"] = role_val
                    if effect == PermissionEffect.ALLOW:
                        if role_val not in allowed_roles:
                            allowed_roles.append(role_val)
                    else:
                        if role_val not in denied_roles:
                            denied_roles.append(role_val)
                    db_records.append(record)

            elif p_type == PrincipalType.USER:
                record["user_id"] = p_id
                if effect == PermissionEffect.ALLOW:
                    if p_id not in allowed_user_ids:
                        allowed_user_ids.append(p_id)
                else:
                    if p_id not in denied_user_ids:
                        denied_user_ids.append(p_id)
                db_records.append(record)

            elif p_type == PrincipalType.GROUP:
                group_val = p_id.lower()
                record["group_id"] = group_val
                if effect == PermissionEffect.ALLOW:
                    if group_val not in allowed_groups:
                        allowed_groups.append(group_val)
                else:
                    if group_val not in denied_groups:
                        denied_groups.append(group_val)
                db_records.append(record)

        # Determine broad access level
        if "PUBLIC" in allowed_roles:
            access_level = "PUBLIC"
        elif allowed_roles:
            access_level = "ROLE"
        elif allowed_groups:
            access_level = "GROUP"
        elif allowed_user_ids:
            access_level = "USER"
        else:
            access_level = "PRIVATE"

        permission_status = "KNOWN"
        # If all permissions were unmapped external domains or rejected anyone links, and no valid allowed user, mark UNKNOWN
        if has_unknown_or_unmapped and not allowed_roles and not allowed_user_ids and not allowed_groups:
            permission_status = "UNKNOWN"

        return NormalizedPermissionSet(
            access_level=access_level,
            permission_status=permission_status,
            allowed_roles=allowed_roles,
            allowed_user_ids=allowed_user_ids,
            allowed_groups=allowed_groups,
            denied_roles=denied_roles,
            denied_user_ids=denied_user_ids,
            denied_groups=denied_groups,
            db_permission_records=db_records,
        )
