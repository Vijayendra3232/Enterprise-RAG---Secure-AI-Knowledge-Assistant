import os
import sys
import pytest
from datetime import timedelta
from fastapi.testclient import TestClient

# Ensure backend directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
backend_dir = os.path.join(parent_dir, "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.auth.password import hash_password, verify_password
from app.auth.jwt import create_access_token, decode_access_token, TokenExpiredError, InvalidTokenError
from app.auth.repository import InMemoryUserRepository
from app.auth.models import UserInDB
from app.auth.service import AuthService
from app.main import app
from app import config

client = TestClient(app)


def test_password_hashing_and_verification():
    raw_pwd = "SuperSecretPassword123!"
    hashed = hash_password(raw_pwd)
    
    assert hashed != raw_pwd
    assert hashed.startswith("$2b$") or hashed.startswith("$2a$")
    assert verify_password(raw_pwd, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False
    assert verify_password("", hashed) is False


def test_jwt_create_and_decode():
    claims = {
        "sub": "test_user_1",
        "tenant_id": "company_a",
        "email": "test@companya.com",
        "name": "Test User",
        "role": "USER"
    }
    token = create_access_token(claims, expires_delta=timedelta(minutes=10))
    assert isinstance(token, str)
    assert len(token) > 20

    payload = decode_access_token(token)
    assert payload.sub == "test_user_1"
    assert payload.tenant_id == "company_a"
    assert payload.email == "test@companya.com"
    assert payload.role == "USER"


def test_jwt_expiration():
    claims = {
        "sub": "expired_user",
        "tenant_id": "company_a",
        "email": "exp@companya.com",
        "name": "Expired User",
        "role": "USER"
    }
    # Expired token in the past
    expired_token = create_access_token(claims, expires_delta=timedelta(seconds=-10))
    with pytest.raises(TokenExpiredError):
        decode_access_token(expired_token)


def test_jwt_tampering_and_invalid_signature():
    claims = {
        "sub": "user_1",
        "tenant_id": "company_a",
        "email": "user1@companya.com",
        "name": "User 1",
        "role": "USER"
    }
    token = create_access_token(claims)
    # Tamper with the token string
    tampered = token[:-5] + "XXXXX"
    with pytest.raises(InvalidTokenError):
        decode_access_token(tampered)


def test_user_repository():
    repo = InMemoryUserRepository()
    user = repo.get_by_email("admin@companya.com")
    assert user is not None
    assert user.role == "ADMIN"
    assert user.tenant_id == "company_a"

    # Inactive user check
    inactive = repo.get_by_email("inactive@companya.com")
    assert inactive is not None
    assert inactive.is_active is False

    # Create new user
    new_user = UserInDB(
        user_id="new_dev_1",
        tenant_id="company_a",
        email="newdev@companya.com",
        name="New Developer",
        role="ENGINEERING",
        is_active=True,
        hashed_password=hash_password("devpass123")
    )
    created = repo.create(new_user)
    assert created.user_id == "new_dev_1"
    assert repo.get_by_id("new_dev_1") is not None


def test_auth_service_authentication():
    service = AuthService()
    
    # Valid login
    user, err = service.authenticate_user("usera@companya.com", "password123")
    assert err is None
    assert user is not None
    assert user.user_id == "user_a"
    assert user.role == "ENGINEERING"

    # Invalid password
    user, err = service.authenticate_user("usera@companya.com", "wrongpass")
    assert user is None
    assert "Invalid" in err

    # Inactive user
    user, err = service.authenticate_user("inactive@companya.com", "inactive123")
    assert user is None
    assert "inactive" in err.lower()

    # Unknown user
    user, err = service.authenticate_user("nonexistent@companya.com", "password123")
    assert user is None
    assert "Invalid" in err


def test_api_login_endpoint():
    # Successful login
    response = client.post("/auth/login", json={
        "email": "usera@companya.com",
        "password": "password123"
    })
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    token = data["access_token"]

    # Use token on /auth/me
    headers = {"Authorization": f"Bearer {token}"}
    me_response = client.get("/auth/me", headers=headers)
    assert me_response.status_code == 200
    me_data = me_response.json()
    assert me_data["user_id"] == "user_a"
    assert me_data["role"] == "ENGINEERING"
    assert me_data["tenant_id"] == "company_a"
    assert "DOCUMENT_READ" in me_data["permissions"]


def test_api_unauthorized_access():
    # Missing token
    resp_no_token = client.get("/auth/me")
    assert resp_no_token.status_code == 401

    # Invalid token
    resp_bad_token = client.get("/auth/me", headers={"Authorization": "Bearer invalid.token.payload"})
    assert resp_bad_token.status_code == 401


def test_admin_user_creation_endpoint():
    # Clean up any leftover records from prior test runs
    from app.storage.database import SessionLocal
    from app.storage.models import User as DBUser
    db = SessionLocal()
    try:
        db.query(DBUser).filter(DBUser.user_id.in_(["api_created_user", "hacker_user"])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    # Admin login
    admin_login = client.post("/auth/login", json={
        "email": "admin@companya.com",
        "password": "admin123"
    })
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Admin creates new user
    create_resp = client.post("/auth/users", json={
        "user_id": "api_created_user",
        "email": "created@companya.com",
        "name": "API Created User",
        "role": "USER",
        "password": "securepassword123"
    }, headers=admin_headers)
    assert create_resp.status_code == 201

    assert create_resp.json()["user_id"] == "api_created_user"

    # Regular user attempting to create user should be forbidden (HTTP 403)
    user_login = client.post("/auth/login", json={
        "email": "usera@companya.com",
        "password": "password123"
    })
    user_token = user_login.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    forbidden_resp = client.post("/auth/users", json={
        "user_id": "hacker_user",
        "email": "hacker@companya.com",
        "name": "Hacker",
        "role": "ADMIN",
        "password": "password"
    }, headers=user_headers)
    assert forbidden_resp.status_code == 403
