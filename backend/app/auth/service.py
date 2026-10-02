"""
service.py — Authentication service coordinating credential verification, token creation, and audit logging.
"""

from typing import Optional, Tuple
from app.auth.models import User, UserInDB, Token
from app.auth.password import verify_password
from app.auth.jwt import create_access_token
from app.auth.repository import get_user_repository, UserRepositoryInterface
from app.security.audit import log_security_event
from app import config


class AuthService:
    """
    Central service for user authentication and token issuance.
    """
    def __init__(self, repository: Optional[UserRepositoryInterface] = None):
        self.repository = repository or get_user_repository()

    def authenticate_user(self, email: str, password: str, request_id: Optional[str] = None) -> Tuple[Optional[User], Optional[str]]:
        """
        Verify credentials. Returns (user, error_message).
        Logs security audit events for successes and failures.
        """
        user_in_db = self.repository.get_by_email(email)
        if not user_in_db:
            log_security_event(
                event_type="LOGIN_FAILURE",
                tenant_id=config.DEFAULT_TENANT_ID,
                user_id="anonymous",
                action="login",
                result="failure",
                details={"reason": "User not found", "email": email},
                request_id=request_id
            )
            return None, "Invalid email or password."

        if not verify_password(password, user_in_db.hashed_password):
            log_security_event(
                event_type="LOGIN_FAILURE",
                tenant_id=user_in_db.tenant_id,
                user_id=user_in_db.user_id,
                action="login",
                result="failure",
                details={"reason": "Invalid password", "email": email},
                request_id=request_id
            )
            return None, "Invalid email or password."

        if not user_in_db.is_active:
            log_security_event(
                event_type="LOGIN_FAILURE",
                tenant_id=user_in_db.tenant_id,
                user_id=user_in_db.user_id,
                action="login",
                result="failure",
                details={"reason": "User account is inactive", "email": email},
                request_id=request_id
            )
            return None, "User account is inactive."

        user = User(
            user_id=user_in_db.user_id,
            tenant_id=user_in_db.tenant_id,
            email=user_in_db.email,
            name=user_in_db.name,
            role=user_in_db.role,
            is_active=user_in_db.is_active,
            groups=user_in_db.groups
        )

        log_security_event(
            event_type="LOGIN_SUCCESS",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            action="login",
            result="success",
            details={"email": email, "role": user.role},
            request_id=request_id
        )

        return user, None

    def issue_token(self, user: User) -> Token:
        """
        Issue a signed JWT access token for an authenticated user.
        """
        claims = {
            "sub": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "groups": user.groups,
        }
        token_str = create_access_token(claims)
        return Token(
            access_token=token_str,
            token_type="bearer",
            expires_in_seconds=config.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        )
