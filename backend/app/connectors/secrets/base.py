"""
base.py — Abstract SecretProvider interface for encrypting and decrypting sensitive connector credentials.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any


class SecretProviderInterface(ABC):
    @abstractmethod
    def encrypt(self, data: str) -> str:
        """Encrypt plaintext string or serialized credentials."""
        pass

    @abstractmethod
    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext back to plaintext."""
        pass

    @abstractmethod
    def encrypt_json(self, config: Dict[str, Any]) -> str:
        """Encrypt configuration dictionary to ciphertext string."""
        pass

    @abstractmethod
    def decrypt_json(self, ciphertext: str) -> Dict[str, Any]:
        """Decrypt ciphertext string back to configuration dictionary."""
        pass
