"""
redaction.py — Safe identity hashing using HMAC-SHA256 and defense-in-depth data redaction.
Guarantees that raw user/tenant identifiers, credentials, tokens, and secrets
never enter logs, metrics, traces, exceptions, or API error payloads.
"""

import os
import hmac
import hashlib
import re
from typing import Any, Dict, List, Optional, Union
from app import config
from app.connectors.secrets import get_secret_provider


# Sensitive field key patterns for defense-in-depth redaction
SENSITIVE_KEY_PATTERNS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "client_secret",
    "private_key",
    "kms",
    "credential",
    "monitoring_token",
    "x-monitoring-token",
    "monitoring_key",
    "x-monitoring-key",
    "x-api-key",
    "query",
    "prompt",
    "response",
    "content",
    "chunk_text",
    "raw_query",
    "raw_acl",
}

# Regex for stripping Bearer tokens and authorization headers from strings
BEARER_TOKEN_REGEX = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE)
MONITORING_HEADER_REGEX = re.compile(r"(X-Monitoring-(?:Token|Key):\s*)[A-Za-z0-9_\-\.]+", re.IGNORECASE)


class SafeIdentityHasher:
    """
    Computes deterministic, non-reversible safe correlation identifiers for tenants and users
    using HMAC-SHA256 backed by the application's SecretProvider.
    """

    _cached_key: Optional[str] = None
    _cached_version: str = "v1"

    def __init__(self, secret_provider: Optional[Any] = None, version: str = "v1"):
        self.secret_provider = secret_provider
        self.version = version
        self._key: Optional[str] = None

    def get_key(self) -> str:
        if self._key:
            return self._key

        key = ""
        if self.secret_provider is not None:
            try:
                key = self.secret_provider.get_secret("telemetry/hmac_key") or ""
            except Exception:
                pass

        if not key:
            try:
                secrets = get_secret_provider()
                key = secrets.get_secret("telemetry/hmac_key") or ""
            except Exception:
                pass

        if not key:
            key = getattr(config, "TELEMETRY_HMAC_KEY", "") or ""

        is_prod = (
            (getattr(config, "APP_ENV", "").lower() == "production")
            or (getattr(config, "ENVIRONMENT", "").lower() == "production")
            or (os.getenv("APP_ENV", "").lower() == "production")
            or (os.getenv("ENVIRONMENT", "").lower() == "production")
        )

        if is_prod:
            if not key or key in ("dev-telemetry-hmac-key", "test-telemetry-hmac-key-do-not-use-in-prod"):
                raise RuntimeError(
                    "Production security violation: Missing telemetry HMAC secret in SecretProvider. "
                    "Fail closed: no fallback salt is permitted."
                )
        else:
            if not key:
                key = "test-telemetry-hmac-key-do-not-use-in-prod"

        self._key = key
        return self._key

    def __repr__(self) -> str:
        return f"<SafeIdentityHasher version={self.version}>"

    @classmethod
    def hash_identifier(cls, raw_id: Optional[str], version: Optional[str] = None, secret_provider: Optional[Any] = None) -> str:
        if not raw_id:
            return ""

        key = ""
        if secret_provider is not None:
            try:
                key = secret_provider.get_secret("telemetry/hmac_key") or ""
            except Exception:
                pass

        if not key:
            key = cls.get_hmac_key()

        v = version or cls._cached_version

        digest = hmac.new(
            key.encode("utf-8"),
            raw_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return f"{v}:{digest[:16]}"

    def hash_tenant_id(self_or_cls, raw_tenant_id: Optional[str] = None) -> str:
        if not raw_tenant_id:
            return "unknown"
        sec_prov = getattr(self_or_cls, "secret_provider", None) if not isinstance(self_or_cls, type) else None
        ver = getattr(self_or_cls, "version", None) if not isinstance(self_or_cls, type) else None
        return SafeIdentityHasher.hash_identifier(raw_tenant_id, version=ver, secret_provider=sec_prov)

    def hash_user_id(self_or_cls, raw_user_id: Optional[str] = None) -> str:
        if not raw_user_id:
            return "anonymous"
        sec_prov = getattr(self_or_cls, "secret_provider", None) if not isinstance(self_or_cls, type) else None
        ver = getattr(self_or_cls, "version", None) if not isinstance(self_or_cls, type) else None
        return SafeIdentityHasher.hash_identifier(raw_user_id, version=ver, secret_provider=sec_prov)

    @classmethod
    def get_hmac_key(cls) -> str:
        if cls._cached_key:
            return cls._cached_key
        return SafeIdentityHasher().get_key()

    @classmethod
    def set_test_key(cls, key: str, version: str = "v1") -> None:
        cls._cached_key = key
        cls._cached_version = version

    @classmethod
    def clear_cached_key(cls) -> None:
        cls._cached_key = None
        cls._cached_version = "v1"


def redact_data(data: Any, max_depth: int = 5) -> Any:
    """
    Recursively redacts sensitive keys and secret values from arbitrary data structures.
    Acts as defense-in-depth; explicit schemas remain the primary security boundary.
    """
    if max_depth <= 0:
        return "[TRUNCATED_DEPTH]"

    if isinstance(data, dict):
        redacted_dict = {}
        for k, v in data.items():
            key_str = str(k).lower()
            if any(pattern in key_str for pattern in SENSITIVE_KEY_PATTERNS):
                redacted_dict[k] = "[REDACTED]"
            else:
                redacted_dict[k] = redact_data(v, max_depth - 1)
        return redacted_dict

    elif isinstance(data, (list, tuple, set)):
        items = [redact_data(item, max_depth - 1) for item in data]
        return type(data)(items) if not isinstance(data, set) else set(items)

    elif isinstance(data, str):
        sanitized = BEARER_TOKEN_REGEX.sub(r"\1[REDACTED]", data)
        sanitized = MONITORING_HEADER_REGEX.sub(r"\1[REDACTED]", sanitized)
        return sanitized

    return data
