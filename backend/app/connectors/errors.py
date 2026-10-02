"""
errors.py — Exception hierarchy for connector operations and permission synchronization.
"""

class ConnectorError(Exception):
    """Base exception for all connector failures."""
    pass


class AuthenticationError(ConnectorError):
    """Raised when external provider credentials are invalid or expired."""
    pass


ConnectorAuthError = AuthenticationError


class PermanentAuthError(AuthenticationError):
    """Raised when authentication failure is permanent (e.g. invalid_grant, consent revoked) and must not be retried."""
    pass


class TokenRefreshError(AuthenticationError):
    """Raised when OAuth token refresh fails."""
    pass


class SourceUnavailableError(ConnectorError):
    """Raised when the external data source / network is unreachable."""
    pass


class RateLimitError(ConnectorError):
    """Raised when external provider rate limits or quotas are exceeded."""
    pass


class PermissionSyncError(ConnectorError):
    """Raised when authoritative ACL retrieval or normalization fails."""
    pass


class MalformedDocumentError(ConnectorError):
    """Raised when source document binary or metadata cannot be processed."""
    pass


class ConnectorNotFoundError(ConnectorError):
    """Raised when a requested connector configuration is not found."""
    pass


class ConcurrentSyncError(ConnectorError):
    """Raised when another sync run is currently active for the connector."""
    pass


class SSRFSecurityError(ConnectorError):
    """Raised when an outbound HTTP request targets a prohibited host, IP, or scheme."""
    pass


class SecretProviderError(ConnectorError):
    """Raised when secret retrieval, creation, or encryption fails in the secret provider."""
    pass


class SecretNotFoundError(SecretProviderError):
    """Raised when a requested secret reference is not found in the secret provider."""
    pass


class SecretDecryptionError(SecretProviderError):
    """Raised when ciphertext decryption or MAC authentication tag verification fails."""
    pass


