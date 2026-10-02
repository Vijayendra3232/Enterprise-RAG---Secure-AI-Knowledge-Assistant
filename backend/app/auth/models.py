"""
models.py — Authentication and User data models.
"""

from typing import Optional, List
from pydantic import BaseModel, Field


class User(BaseModel):
    """
    User model representing an authenticated identity within a tenant.
    """
    user_id: str
    tenant_id: str
    email: str
    name: str
    role: str
    is_active: bool = True
    groups: List[str] = Field(default_factory=list)


class UserInDB(User):
    """
    Internal user model with hashed password for verification.
    """
    hashed_password: str


class LoginRequest(BaseModel):
    """
    Request model for username/email + password login.
    """
    email: str
    password: str


class Token(BaseModel):
    """
    OAuth2 / JWT token response model.
    """
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int


class TokenPayload(BaseModel):
    """
    Structured payload decoded from a cryptographically verified JWT token.
    """
    sub: str  # user_id
    tenant_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    groups: List[str] = Field(default_factory=list)
    iss: Optional[str] = None
    aud: Optional[str] = None
    exp: int
    iat: Optional[int] = None
    nbf: Optional[int] = None
