"""
auth.py — Authentication API endpoints for user login, token issuance, and profile retrieval.
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel

from app.auth.models import User, Token, LoginRequest, UserInDB
from app.auth.service import AuthService
from app.auth.dependencies import get_current_user
from app.auth.repository import get_user_repository
from app.auth.password import hash_password
from app.authorization.policy import require_permission, AuthorizationPolicy
from app.authorization.permissions import Permission
from app.authorization.roles import Role
from app.security.audit import log_security_event

router = APIRouter(prefix="/auth", tags=["authentication"])
auth_service = AuthService()


class UserProfileResponse(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    name: str
    role: str
    permissions: List[str]


class CreateUserRequest(BaseModel):
    user_id: str
    email: str
    name: str
    role: str
    password: str
    tenant_id: Optional[str] = None


@router.post("/login", response_model=Token, status_code=status.HTTP_200_OK)
async def login(request: Request, login_data: LoginRequest):
    """
    Authenticate user with email and password.
    Returns signed JWT access token.
    """
    request_id = request.headers.get("X-Request-ID")
    user, error_msg = auth_service.authenticate_user(
        email=login_data.email,
        password=login_data.password,
        request_id=request_id
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_msg or "Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return auth_service.issue_token(user)


@router.get("/me", response_model=UserProfileResponse, status_code=status.HTTP_200_OK)
async def get_my_profile(current_user: User = Depends(get_current_user)):
    """
    Get the authenticated user's profile and resolved permissions.
    """
    perms = AuthorizationPolicy.get_permissions(current_user.role)
    return UserProfileResponse(
        user_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
        email=current_user.email,
        name=current_user.name,
        role=current_user.role,
        permissions=[p.value for p in perms]
    )


@router.post("/users", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(
    new_user_data: CreateUserRequest,
    current_admin: User = Depends(require_permission(Permission.USER_MANAGE))
):
    """
    Admin-only endpoint to register a new user in the caller's tenant.
    """
    # Force new user to belong to current admin's tenant (strict isolation)
    tenant_id = current_admin.tenant_id
    if current_admin.role.upper() == Role.ADMIN.value and new_user_data.tenant_id:
        tenant_id = new_user_data.tenant_id

    user_in_db = UserInDB(
        user_id=new_user_data.user_id,
        tenant_id=tenant_id,
        email=new_user_data.email,
        name=new_user_data.name,
        role=new_user_data.role.upper(),
        is_active=True,
        hashed_password=hash_password(new_user_data.password)
    )

    repo = get_user_repository()
    try:
        created = repo.create(user_in_db)
        log_security_event(
            event_type="USER_CREATED",
            tenant_id=tenant_id,
            user_id=current_admin.user_id,
            action="create_user",
            result="success",
            details={"created_user_id": created.user_id, "role": created.role}
        )
        return User(
            user_id=created.user_id,
            tenant_id=created.tenant_id,
            email=created.email,
            name=created.name,
            role=created.role,
            is_active=created.is_active,
            groups=created.groups
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
