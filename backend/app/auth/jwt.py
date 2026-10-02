"""
jwt.py — JSON Web Token creation, cryptographic decoding, and semantic validation.
Enforces strict signature verification, server-controlled algorithm allowlist,
issuer allowlisting, audience validation, tenant isolation, and clock-skew tolerance.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Union
import jwt
from pydantic import ValidationError

from app import config
from app.auth.models import TokenPayload


class JWTError(Exception):
    """Base exception for JWT processing failures."""
    pass


class TokenExpiredError(JWTError):
    """Raised when token has expired."""
    pass


class InvalidTokenError(JWTError):
    """Raised when token is malformed, invalid signature, or claims are missing."""
    pass


class DisallowedAlgorithmError(InvalidTokenError):
    """Raised when token uses an algorithm outside the server-configured allowlist."""
    pass


class UntrustedIssuerError(InvalidTokenError):
    """Raised when token issuer (iss) does not match trusted identity providers."""
    pass


class InvalidAudienceError(InvalidTokenError):
    """Raised when token audience (aud) does not match application client ID."""
    pass


class TenantMismatchError(InvalidTokenError):
    """Raised when token tenant does not match expected tenant boundary."""
    pass


def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    algorithm: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> str:
    """
    Generate a cryptographically signed JWT token containing identity and authorization claims.
    Uses server-configured algorithm and signing key.
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.setdefault("iss", issuer or getattr(config, "JWT_ISSUER", "enterprise-rag-auth"))
    to_encode.setdefault("aud", audience or getattr(config, "JWT_AUDIENCE", "enterprise-rag-api"))
    to_encode.setdefault("tenant_id", getattr(config, "DEFAULT_TENANT_ID", "default_tenant"))
    to_encode["iat"] = int(now.timestamp())
    to_encode["nbf"] = int(now.timestamp())
    to_encode["exp"] = int(expire.timestamp())

    sign_alg = algorithm or getattr(config, "JWT_ALGORITHM", "HS256")
    sign_key = secret_key or config.JWT_SECRET_KEY

    encoded_jwt = jwt.encode(
        to_encode,
        sign_key,
        algorithm=sign_alg,
    )
    return encoded_jwt


def decode_access_token(
    token: str,
    expected_issuer: Optional[str] = None,
    expected_audience: Optional[str] = None,
    expected_tenant_id: Optional[str] = None,
    verify_signature: bool = True,
    verify_issuer: bool = True,
    verify_audience: bool = True,
    leeway: Optional[int] = None,
) -> TokenPayload:
    """
    Cryptographically verify signature and validate semantic claims of a JWT access token.
    Enforces server-controlled algorithm allowlist, signature verification,
    trusted issuer matching, audience matching, expiration / nbf checks, and tenant consistency.
    """
    if not verify_signature:
        # Mandatory security invariant: Unverified decoding is NEVER permitted for authorization
        raise InvalidTokenError("Unverified token decoding is strictly prohibited.")

    allowed_algorithms = getattr(config, "JWT_ALLOWED_ALGORITHMS", [getattr(config, "JWT_ALGORITHM", "HS256")])
    trusted_issuers = getattr(config, "AUTH_TRUSTED_ISSUERS", [getattr(config, "JWT_ISSUER", "enterprise-rag-auth")])
    target_audience = expected_audience or getattr(config, "JWT_AUDIENCE", "enterprise-rag-api")
    leeway_seconds = leeway if leeway is not None else getattr(config, "JWT_LEEWAY_SECONDS", 0)

    # 1. Inspect unverified header for strict algorithm allowlisting
    try:
        header = jwt.get_unverified_header(token)
    except Exception as e:
        raise InvalidTokenError(f"Malformed JWT header: {str(e)}") from e

    token_alg = header.get("alg")
    if not token_alg or token_alg.lower() == "none":
        raise DisallowedAlgorithmError("Unsigned JWTs with alg='none' are strictly prohibited.")

    if token_alg not in allowed_algorithms:
        raise DisallowedAlgorithmError(
            f"JWT algorithm '{token_alg}' is not in the server-configured allowlist: {allowed_algorithms}"
        )

    try:
        # 2. Cryptographic signature and temporal claims validation via PyJWT
        payload = jwt.decode(
            token,
            config.JWT_SECRET_KEY,
            algorithms=allowed_algorithms,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": False,  # Evaluated explicitly below for precise error attribution
                "verify_iss": False,  # Evaluated explicitly below for precise error attribution
            },
            leeway=leeway_seconds,
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError("Token has expired.") from e
    except jwt.ImmatureSignatureError as e:
        raise InvalidTokenError("Token not yet valid (nbf claim).") from e
    except jwt.InvalidSignatureError as e:
        raise InvalidTokenError("Invalid token cryptographic signature.") from e
    except (jwt.InvalidTokenError, jwt.DecodeError) as e:
        raise InvalidTokenError(f"Malformed or invalid token: {str(e)}") from e

    # 3. Issuer Validation
    token_iss = payload.get("iss")
    if verify_issuer:
        if not token_iss:
            raise UntrustedIssuerError("Missing token issuer (iss) claim.")
        if expected_issuer:
            if token_iss != expected_issuer:
                raise UntrustedIssuerError(f"Untrusted token issuer '{token_iss}'; expected '{expected_issuer}'.")
        elif token_iss not in trusted_issuers:
            raise UntrustedIssuerError(f"Untrusted token issuer '{token_iss}'.")

    # 4. Audience Validation
    token_aud = payload.get("aud")
    if verify_audience:
        if not token_aud:
            raise InvalidAudienceError("Missing token audience (aud) claim.")
        token_audiences = [token_aud] if isinstance(token_aud, str) else list(token_aud)
        if target_audience and target_audience not in token_audiences:
            raise InvalidAudienceError(f"Token audience mismatch: '{token_aud}' does not match expected audience '{target_audience}'.")

    # 5. Tenant Validation
    token_tenant = payload.get("tenant_id")
    if expected_tenant_id and token_tenant != expected_tenant_id:
        raise TenantMismatchError(f"Token tenant '{token_tenant}' does not match expected tenant '{expected_tenant_id}'.")

    try:
        return TokenPayload(**payload)
    except ValidationError as e:
        raise InvalidTokenError(f"Token payload structure is invalid: {str(e)}") from e
