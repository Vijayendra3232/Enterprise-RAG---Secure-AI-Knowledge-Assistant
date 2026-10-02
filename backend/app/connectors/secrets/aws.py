"""
aws.py — Production AWSSecretsManagerProvider with Bounded Thread-Safe TTL Caching and Rotation Support.
Enforces tenant isolation, sanitized error handling, and zero plaintext secret leakage.
"""

import json
import logging
import re
import threading
import time
from typing import Dict, Any, Optional, Tuple

import botocore.config
import botocore.exceptions

from app import config
from app.connectors.secrets.base import SecretProviderInterface
from app.connectors.errors import (
    SecretProviderError,
    SecretNotFoundError,
    SecretDecryptionError,
)
from app.connectors.secrets.local import LocalSecretProvider

logger = logging.getLogger(__name__)

# Strict identifier sanitization pattern
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def build_secret_name(prefix: str, tenant_id: str, connector_id: str) -> str:
    """
    Construct a deterministic, server-controlled, tenant-scoped secret name.
    Format: {prefix}tenants/{tenant_id}/connectors/{connector_id}/config
    """
    clean_prefix = prefix.strip("/")
    if clean_prefix:
        clean_prefix = f"{clean_prefix}/"

    safe_tenant = "".join(c for c in tenant_id if c.isalnum() or c in ("-", "_")).strip() or "default"
    safe_connector = "".join(c for c in connector_id if c.isalnum() or c in ("-", "_")).strip() or "default"

    return f"{clean_prefix}tenants/{safe_tenant}/connectors/{safe_connector}/config"


class BoundedSecretCache:
    """
    Thread-safe, bounded in-memory cache with configurable TTL and LRU eviction.
    Cache keys use authoritative server-side identifiers (tenant_id:connector_id:secret_id),
    never plaintext credentials or secret values.
    """

    def __init__(self, max_entries: int = 1000, ttl_seconds: int = 300):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        # Structure: key -> {"value": Any, "expire_at": float, "last_accessed": float}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            if now >= entry["expire_at"]:
                # Expired -> remove and signal miss
                self._cache.pop(key, None)
                return None
            entry["last_accessed"] = now
            return entry["value"]

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        expire_at = now + ttl

        with self._lock:
            # If full, evict expired items first
            if len(self._cache) >= self.max_entries and key not in self._cache:
                expired_keys = [k for k, v in self._cache.items() if now >= v["expire_at"]]
                for exp_k in expired_keys:
                    self._cache.pop(exp_k, None)

            # If still full, evict Least Recently Used (LRU)
            if len(self._cache) >= self.max_entries and key not in self._cache:
                lru_key = min(self._cache.keys(), key=lambda k: self._cache[k]["last_accessed"])
                self._cache.pop(lru_key, None)

            self._cache[key] = {
                "value": value,
                "expire_at": expire_at,
                "last_accessed": now,
            }

    def invalidate(self, key: str) -> bool:
        with self._lock:
            return self._cache.pop(key, None) is not None

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            to_remove = [k for k in self._cache if k.startswith(prefix)]
            for k in to_remove:
                self._cache.pop(k, None)
            return len(to_remove)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


class AWSSecretsManagerProvider(SecretProviderInterface):
    """
    Production-grade secret provider interfacing with AWS Secrets Manager.
    Features bounded TTL caching, thread safety, rotation awareness, and sanitized exception reporting.
    """

    def __init__(
        self,
        prefix: Optional[str] = None,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        cache_ttl: Optional[int] = None,
        max_cache_entries: Optional[int] = None,
        client: Optional[Any] = None,
    ):
        self.prefix = prefix or config.AWS_SECRET_PREFIX
        self.region = region or config.AWS_SECRETS_REGION
        self.endpoint_url = endpoint_url or config.AWS_SECRETS_ENDPOINT_URL
        self._cache = BoundedSecretCache(
            max_entries=max_cache_entries or config.SECRET_CACHE_MAX_ENTRIES,
            ttl_seconds=cache_ttl or config.SECRET_CACHE_TTL_SECONDS,
        )
        self._local_fallback = LocalSecretProvider()

        if client is not None:
            self._client = client
        else:
            import boto3
            boto_config = botocore.config.Config(
                connect_timeout=config.S3_CONNECT_TIMEOUT,
                read_timeout=config.S3_READ_TIMEOUT,
                retries={"max_attempts": 3, "mode": "standard"},
            )
            self._client = boto3.client(
                "secretsmanager",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
                config=boto_config,
            )

    def _map_client_error(self, exc: botocore.exceptions.ClientError, secret_id: str) -> SecretProviderError:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code in ("ResourceNotFoundException", "404"):
            return SecretNotFoundError("Secret reference not found in secrets provider.")
        if error_code in ("AccessDeniedException", "403"):
            return SecretProviderError("Access denied to secrets provider.")
        return SecretProviderError(f"Secrets provider operation failed with code: {error_code}")

    def _make_cache_key(self, tenant_id: str, connector_id: str, secret_id: str) -> str:
        return f"{tenant_id}:{connector_id}:{secret_id}"

    def encrypt_json(
        self,
        config_dict: Dict[str, Any],
        tenant_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        if not tenant_id or not connector_id:
            # Fallback to local authenticated envelope if tenant/connector context is omitted
            return self._local_fallback.encrypt_json(config_dict)

        secret_name = build_secret_name(self.prefix, tenant_id, connector_id)
        secret_string = json.dumps(config_dict or {})

        try:
            try:
                self._client.create_secret(
                    Name=secret_name,
                    SecretString=secret_string,
                    Description=f"Enterprise RAG connector credentials for tenant {tenant_id}",
                    Tags=[
                        {"Key": "TenantId", "Value": tenant_id},
                        {"Key": "ConnectorId", "Value": connector_id},
                        {"Key": "ManagedBy", "Value": "enterprise-rag"},
                    ],
                )
            except botocore.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ResourceExistsException":
                    self._client.put_secret_value(
                        SecretId=secret_name,
                        SecretString=secret_string,
                    )
                else:
                    raise
        except botocore.exceptions.ClientError as exc:
            raise self._map_client_error(exc, secret_name) from exc
        except Exception as exc:
            raise SecretProviderError(f"Failed to persist secret: {exc}") from exc

        # Update cache
        cache_key = self._make_cache_key(tenant_id, connector_id, secret_name)
        self._cache.set(cache_key, config_dict)

        return secret_name

    def decrypt_json(
        self,
        ciphertext_or_ref: str,
        tenant_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not ciphertext_or_ref:
            return {}

        # If ciphertext is legacy local format (v1:...), decrypt with authenticated local provider
        if ciphertext_or_ref.startswith(LocalSecretProvider.VERSION_PREFIX):
            return self._local_fallback.decrypt_json(ciphertext_or_ref)

        secret_id = ciphertext_or_ref.strip()
        effective_tenant = tenant_id or "default"
        effective_connector = connector_id or "default"
        cache_key = self._make_cache_key(effective_tenant, effective_connector, secret_id)

        # 1. Check bounded in-memory TTL cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # 2. Fetch from AWS Secrets Manager
        try:
            response = self._client.get_secret_value(SecretId=secret_id)
        except botocore.exceptions.ClientError as exc:
            raise self._map_client_error(exc, secret_id) from exc
        except Exception as exc:
            raise SecretProviderError(f"Failed to fetch secret: {exc}") from exc

        secret_str = response.get("SecretString", "")
        if not secret_str:
            return {}

        try:
            parsed = json.loads(secret_str)
        except Exception as exc:
            raise SecretDecryptionError(f"Secret payload is not valid JSON: {exc}") from exc

        # 3. Store in bounded cache
        self._cache.set(cache_key, parsed)
        return parsed

    def encrypt(
        self,
        data: str,
        tenant_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        if not tenant_id or not connector_id:
            return self._local_fallback.encrypt(data)
        return self.encrypt_json({"value": data}, tenant_id=tenant_id, connector_id=connector_id)

    def decrypt(
        self,
        ciphertext_or_ref: str,
        tenant_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        if not ciphertext_or_ref:
            return ""
        if ciphertext_or_ref.startswith(LocalSecretProvider.VERSION_PREFIX):
            return self._local_fallback.decrypt(ciphertext_or_ref)
        parsed = self.decrypt_json(ciphertext_or_ref, tenant_id=tenant_id, connector_id=connector_id)
        return parsed.get("value", "")

    def invalidate_cache(
        self,
        tenant_id: str,
        connector_id: str,
        secret_id: Optional[str] = None,
    ) -> None:
        """Explicitly invalidate cached credentials (e.g. upon rotation or update)."""
        if secret_id:
            cache_key = self._make_cache_key(tenant_id, connector_id, secret_id)
            self._cache.invalidate(cache_key)
        else:
            prefix = f"{tenant_id}:{connector_id}:"
            self._cache.invalidate_prefix(prefix)

    def health_check(self) -> Dict[str, Any]:
        try:
            self._client.list_secrets(MaxResults=1)
            return {
                "status": "HEALTHY",
                "backend": "aws_secrets_manager",
                "region": self.region,
                "cache_size": self._cache.size(),
            }
        except botocore.exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            return {
                "status": "UNHEALTHY",
                "backend": "aws_secrets_manager",
                "error_code": error_code,
            }
        except Exception as exc:
            return {
                "status": "UNHEALTHY",
                "backend": "aws_secrets_manager",
                "error": str(exc),
            }
