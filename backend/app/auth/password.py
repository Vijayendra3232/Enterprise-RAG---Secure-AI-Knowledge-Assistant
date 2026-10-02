"""
password.py — Secure password hashing and verification using bcrypt.
"""

import bcrypt


def hash_password(password: str) -> str:
    """
    Hash a plaintext password using bcrypt with a generated salt.
    Never returns or logs plaintext passwords.
    """
    if not password:
        raise ValueError("Password cannot be empty.")
    salt = bcrypt.gensalt(rounds=12)
    hashed_bytes = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed_bytes.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a bcrypt hash.
    Constant-time comparison prevents timing attacks.
    """
    if not plain_password or not hashed_password:
        return False
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8")
        )
    except Exception:
        return False
