"""
secrets package — Secret provider abstractions, local encryption, and AWS Secrets Manager implementations.
"""

from app.connectors.secrets.base import SecretProviderInterface
from app.connectors.secrets.local import LocalSecretProvider
from app.connectors.secrets.aws import AWSSecretsManagerProvider, BoundedSecretCache, build_secret_name
from app.connectors.secrets.factory import get_secret_provider, reset_secret_provider

__all__ = [
    "SecretProviderInterface",
    "LocalSecretProvider",
    "AWSSecretsManagerProvider",
    "BoundedSecretCache",
    "build_secret_name",
    "get_secret_provider",
    "reset_secret_provider",
]
