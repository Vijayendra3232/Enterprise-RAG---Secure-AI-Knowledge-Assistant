"""
dependencies.py — FastAPI dependencies for authentication, token validation, and trusted authorization context resolution.
Enforces that only cryptographically verified, correct-issuer, correct-audience, correct-tenant identities
and authoritative group memberships participate in security decisions.
"""

from typing import Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app import config
from app.auth.models import User
from app.auth.jwt import (
    decode_access_token,
    JWTError,
    TokenExpiredError,
    DisallowedAlgorithmError,
    UntrustedIssuerError,
    InvalidAudienceError,
    TenantMismatchError,
    InvalidTokenError,
)
from app.auth.repository import get_user_repository
from app.authorization.context import AuthorizationContext


# Bearer token extractor
security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme)
) -> User:
    """
    Extracts and cryptographically validates the JWT Bearer token from the Authorization header.
    Resolves the authenticated user from the authoritative repository and derives trusted group claims.
    """
    if not credentials or not credentials.credentials:
        # Development bypass if authentication is explicitly disabled
        if not config.AUTH_ENABLED:
            user_in_db = get_user_repository().get_by_id("admin_a")
            if user_in_db:
                return User(
                    user_id=user_in_db.user_id,
                    tenant_id=user_in_db.tenant_id,
                    email=user_in_db.email,
                    name=user_in_db.name,
                    role=user_in_db.role,
                    is_active=user_in_db.is_active,
                    groups=user_in_db.groups or []
                )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token = credentials.credentials

    # Track authentication attempt
    from app.observability.metrics import (
        auth_attempts_total,
        auth_success_total,
        auth_failures_total,
        jwt_rejections_total,
    )
    from app.observability.events import emit_security_event
    from app.observability.redaction import SafeIdentityHasher
    from app.observability.context import set_request_context

    auth_attempts_total.inc(labels={"auth_method": "bearer_jwt"})

    # 1. Cryptographic and semantic token validation (signature, algorithm, issuer, audience, exp, nbf, iat)
    try:
        payload = decode_access_token(
            token=token,
            verify_signature=True,
            verify_issuer=True,
            verify_audience=True,
        )
    except TokenExpiredError as e:
        jwt_rejections_total.inc(labels={"rejection_reason": "token_expired"})
        auth_failures_total.inc(labels={"error_class": "TokenExpiredError"})
        emit_security_event("JWT_EXPIRATION_FAILURE", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except DisallowedAlgorithmError as e:
        jwt_rejections_total.inc(labels={"rejection_reason": "disallowed_algorithm"})
        auth_failures_total.inc(labels={"error_class": "DisallowedAlgorithmError"})
        emit_security_event("INVALID_JWT_ALGORITHM", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token claim validation failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except UntrustedIssuerError as e:
        jwt_rejections_total.inc(labels={"rejection_reason": "untrusted_issuer"})
        auth_failures_total.inc(labels={"error_class": "UntrustedIssuerError"})
        emit_security_event("JWT_ISSUER_FAILURE", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token claim validation failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except InvalidAudienceError as e:
        jwt_rejections_total.inc(labels={"rejection_reason": "invalid_audience"})
        auth_failures_total.inc(labels={"error_class": "InvalidAudienceError"})
        emit_security_event("JWT_AUDIENCE_FAILURE", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token claim validation failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except TenantMismatchError as e:
        jwt_rejections_total.inc(labels={"rejection_reason": "tenant_mismatch"})
        auth_failures_total.inc(labels={"error_class": "TenantMismatchError"})
        emit_security_event("TENANT_MISMATCH", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token claim validation failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except (InvalidTokenError, JWTError) as e:
        jwt_rejections_total.inc(labels={"rejection_reason": "invalid_token"})
        auth_failures_total.inc(labels={"error_class": "InvalidTokenError"})
        emit_security_event("JWT_SIGNATURE_FAILURE", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or untrusted token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # 2. Authoritative User Lookup
    user_repo = get_user_repository()
    user_in_db = user_repo.get_by_id(payload.sub)

    if not user_in_db:
        auth_failures_total.inc(labels={"error_class": "UserNotFound"})
        emit_security_event("AUTH_FAILURE", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user no longer exists.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if not user_in_db.is_active:
        auth_failures_total.inc(labels={"error_class": "UserInactive"})
        emit_security_event("AUTH_FAILURE", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # 3. Tenant Boundary Verification
    if payload.tenant_id and user_in_db.tenant_id != payload.tenant_id:
        auth_failures_total.inc(labels={"error_class": "TenantMismatch"})
        emit_security_event("TENANT_MISMATCH", action="authenticate", result="failure")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cross-tenant identity mismatch.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # 4. Authoritative Group Membership Resolution
    # PostgreSQL stores synchronized application authorization state.
    # Synchronized state in the database repository is authoritative and strictly overrides
    # any stale group claims in JWT tokens.
    if user_in_db.groups is not None:
        trusted_groups = list(user_in_db.groups)
    elif payload.groups:
        trusted_groups = list(payload.groups)
    else:
        trusted_groups = []

    auth_success_total.inc(labels={"auth_method": "bearer_jwt"})

    # Bind safe HMAC identifiers to request context
    safe_t = SafeIdentityHasher.hash_tenant_id(user_in_db.tenant_id)
    safe_u = SafeIdentityHasher.hash_user_id(user_in_db.user_id)
    set_request_context(safe_tenant_id=safe_t, safe_user_id=safe_u)

    return User(
        user_id=user_in_db.user_id,
        tenant_id=user_in_db.tenant_id,
        email=user_in_db.email,
        name=user_in_db.name,
        role=user_in_db.role,
        is_active=user_in_db.is_active,
        groups=trusted_groups,
    )


async def get_current_auth_context(
    current_user: User = Depends(get_current_user)
) -> AuthorizationContext:
    """
    Constructs an immutable AuthorizationContext from the cryptographically verified user.
    Client request bodies, headers, or parameters have ZERO authority to modify groups.
    """
    from app.authorization.policy import AuthorizationPolicy
    permissions = AuthorizationPolicy.get_permissions(current_user.role)
    return AuthorizationContext(
        user_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
        permissions=permissions,
        groups=current_user.groups
    )
