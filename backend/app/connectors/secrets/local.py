"""
local.py — Local secret provider using authenticated symmetric keystream encryption + HMAC-SHA256.
Pure Python standard library implementation with zero external dependencies.
Never stores connector credentials, API keys, or OAuth tokens in plaintext.
"""

import base64
import hashlib
import hmac
import json
import secrets
from typing import Dict, Any, Optional

from app import config
from app.connectors.secrets.base import SecretProviderInterface
from app.connectors.errors import SecretDecryptionError


class LocalSecretProvider(SecretProviderInterface):
    """
    Production-hardened authenticated symmetric encryption provider using PBKDF2-HMAC-SHA256,
    AES-like counter keystream, CSPRNG random IVs, and Encrypt-then-MAC (HMAC-SHA256).
    Payloads are version-tagged (v1:...) and raise typed SecretDecryptionError on any integrity failure.
    """

    VERSION_PREFIX = "v1:"
    MIN_PAYLOAD_BYTES = 48  # 16 bytes IV + 32 bytes HMAC-SHA256

    def __init__(self, master_key: Optional[str] = None):
        raw_key = (
            master_key
            or getattr(config, "SECRET_KEY", None)
            or "enterprise-rag-default-secret-key-32b"
        ).encode("utf-8")
        # Standard PBKDF2 key derivation (100,000 iterations) with fixed domain separation salt
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            raw_key,
            b"enterprise-rag-secrets-v1",
            100_000,
            dklen=64,
        )
        self._enc_key = derived[:32]
        self._auth_key = derived[32:64]

    def _generate_keystream(self, iv: bytes, length: int) -> bytes:
        """Generate deterministic pseudo-random keystream of specified byte length using counter mode."""
        blocks = []
        counter = 0
        while len(b"".join(blocks)) < length:
            ctr_bytes = counter.to_bytes(4, byteorder="big")
            block = hmac.new(self._enc_key, iv + ctr_bytes, hashlib.sha256).digest()
            blocks.append(block)
            counter += 1
        return b"".join(blocks)[:length]

    def encrypt(self, data: str) -> str:
        if not data:
            return ""
        plaintext_bytes = data.encode("utf-8")
        iv = secrets.token_bytes(16)
        keystream = self._generate_keystream(iv, len(plaintext_bytes))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext_bytes, keystream))

        # Encrypt-then-MAC authentication tag
        mac = hmac.new(self._auth_key, iv + ciphertext, hashlib.sha256).digest()

        # Format: IV (16 bytes) + MAC (32 bytes) + Ciphertext (N bytes)
        payload = iv + mac + ciphertext
        b64_payload = base64.urlsafe_b64encode(payload).decode("utf-8")
        return f"{self.VERSION_PREFIX}{b64_payload}"

    def decrypt(self, ciphertext_b64: str) -> str:
        if not ciphertext_b64:
            return ""
        raw_str = ciphertext_b64.strip()

        # Strip version prefix if present
        if raw_str.startswith(self.VERSION_PREFIX):
            b64_part = raw_str[len(self.VERSION_PREFIX):]
        else:
            b64_part = raw_str

        try:
            payload = base64.urlsafe_b64decode(b64_part.encode("utf-8"))
        except Exception as exc:
            raise SecretDecryptionError(f"Malformed base64 ciphertext payload: {exc}") from exc

        if len(payload) < self.MIN_PAYLOAD_BYTES:
            raise SecretDecryptionError(
                f"Ciphertext payload length ({len(payload)} bytes) is below minimum required {self.MIN_PAYLOAD_BYTES} bytes."
            )

        iv = payload[:16]
        mac = payload[16:48]
        ciphertext = payload[48:]

        # Verify HMAC-SHA256 MAC tag
        expected_mac = hmac.new(self._auth_key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            raise SecretDecryptionError("Ciphertext authentication failed (invalid MAC tag or incorrect master key).")

        keystream = self._generate_keystream(iv, len(ciphertext))
        plaintext_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream))

        try:
            return plaintext_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretDecryptionError(f"Decrypted secret bytes could not be decoded as UTF-8: {exc}") from exc

    def encrypt_json(self, config_dict: Dict[str, Any]) -> str:
        payload = json.dumps(config_dict or {})
        return self.encrypt(payload)

    def decrypt_json(self, ciphertext: str) -> Dict[str, Any]:
        if not ciphertext:
            return {}
        decrypted_str = self.decrypt(ciphertext)
        if not decrypted_str:
            return {}
        try:
            return json.loads(decrypted_str)
        except Exception as exc:
            raise SecretDecryptionError(f"Decrypted secret payload is not valid JSON: {exc}") from exc


# Global singleton helper
_default_secret_provider: Optional[LocalSecretProvider] = None


def get_secret_provider() -> SecretProviderInterface:
    global _default_secret_provider
    if _default_secret_provider is None:
        _default_secret_provider = LocalSecretProvider()
    return _default_secret_provider
