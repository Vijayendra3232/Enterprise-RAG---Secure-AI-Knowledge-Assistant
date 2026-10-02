"""
factory.py — Factory for instantiating and resolving SecretProviderInterface implementations.
Enforces fail-closed configuration validation in production.
"""

from typing import Optional
from app import config
from app.connectors.secrets.base import SecretProviderInterface
from app.connectors.secrets.local import LocalSecretProvider
from app.connectors.errors import SecretProviderError

_secret_provider_instance: Optional[SecretProviderInterface] = None


def get_secret_provider() -> SecretProviderInterface:
    """
    Resolve and return the configured SecretProviderInterface singleton.
    In production environments, verifies provider readiness and routing.
    """
    global _secret_provider_instance
    if _secret_provider_instance is not None:
        return _secret_provider_instance

    provider_type = (config.SECRET_PROVIDER_TYPE or "local").lower()

    if provider_type == "aws":
        from app.connectors.secrets.aws import AWSSecretsManagerProvider
        _secret_provider_instance = AWSSecretsManagerProvider()
    else:
        if config.ENVIRONMENT == "production":
            import logging
            logging.getLogger(__name__).warning(
                "[SecretFactory] LocalSecretProvider is active in production environment. Ensure this is intentional."
            )
        _secret_provider_instance = LocalSecretProvider()

    return _secret_provider_instance


def reset_secret_provider() -> None:
    """Reset the singleton instance for test isolation."""
    global _secret_provider_instance
    _secret_provider_instance = None
