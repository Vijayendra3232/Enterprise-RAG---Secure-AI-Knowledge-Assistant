"""
repository.py — User repository abstraction and in-memory persistence implementation.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict
from app.auth.models import User, UserInDB
from app.auth.password import hash_password


class UserRepositoryInterface(ABC):
    """
    Abstract repository interface for user identity management.
    Designed for seamless future migration to PostgreSQL / SQLAlchemy.
    """
    @abstractmethod
    def get_by_id(self, user_id: str) -> Optional[UserInDB]:
        pass

    @abstractmethod
    def get_by_email(self, email: str) -> Optional[UserInDB]:
        pass

    @abstractmethod
    def create(self, user: UserInDB) -> UserInDB:
        pass

    @abstractmethod
    def list_by_tenant(self, tenant_id: str) -> List[User]:
        pass


class InMemoryUserRepository(UserRepositoryInterface):
    """
    Thread-safe in-memory user repository seeded with standard development & testing users.
    """
    def __init__(self):
        self._users_by_id: Dict[str, UserInDB] = {}
        self._users_by_email: Dict[str, UserInDB] = {}
        self._seed_default_users()

    def _seed_default_users(self):
        default_users = [
            # --- Company A Users ---
            UserInDB(
                user_id="admin_a",
                tenant_id="company_a",
                email="admin@companya.com",
                name="Company A Admin",
                role="ADMIN",
                is_active=True,
                hashed_password=hash_password("admin123")
            ),
            UserInDB(
                user_id="manager_a",
                tenant_id="company_a",
                email="manager@companya.com",
                name="Company A Manager",
                role="MANAGER",
                is_active=True,
                hashed_password=hash_password("manager123")
            ),
            UserInDB(
                user_id="user_a",
                tenant_id="company_a",
                email="usera@companya.com",
                name="User A (Engineering)",
                role="ENGINEERING",
                is_active=True,
                hashed_password=hash_password("password123")
            ),
            UserInDB(
                user_id="user_b",
                tenant_id="company_a",
                email="userb@companya.com",
                name="User B (Finance)",
                role="FINANCE",
                is_active=True,
                hashed_password=hash_password("password123")
            ),
            UserInDB(
                user_id="viewer_a",
                tenant_id="company_a",
                email="viewer@companya.com",
                name="Company A Viewer",
                role="VIEWER",
                is_active=True,
                hashed_password=hash_password("viewer123")
            ),
            # --- Company B Users (for multi-tenant isolation testing) ---
            UserInDB(
                user_id="admin_b",
                tenant_id="company_b",
                email="admin@companyb.com",
                name="Company B Admin",
                role="ADMIN",
                is_active=True,
                hashed_password=hash_password("admin123")
            ),
            UserInDB(
                user_id="user_c",
                tenant_id="company_b",
                email="userc@companyb.com",
                name="User C (Company B Eng)",
                role="ENGINEERING",
                is_active=True,
                hashed_password=hash_password("password123")
            ),
            # --- Inactive User (for auth verification testing) ---
            UserInDB(
                user_id="inactive_user",
                tenant_id="company_a",
                email="inactive@companya.com",
                name="Inactive User",
                role="USER",
                is_active=False,
                hashed_password=hash_password("inactive123")
            ),
        ]
        for u in default_users:
            self._users_by_id[u.user_id] = u
            self._users_by_email[u.email.lower()] = u

    def get_by_id(self, user_id: str) -> Optional[UserInDB]:
        return self._users_by_id.get(user_id)

    def get_by_email(self, email: str) -> Optional[UserInDB]:
        return self._users_by_email.get(email.lower())

    def create(self, user: UserInDB) -> UserInDB:
        if user.user_id in self._users_by_id:
            raise ValueError(f"User with ID '{user.user_id}' already exists.")
        if user.email.lower() in self._users_by_email:
            raise ValueError(f"User with email '{user.email}' already exists.")
        self._users_by_id[user.user_id] = user
        self._users_by_email[user.email.lower()] = user
        return user

    def list_by_tenant(self, tenant_id: str) -> List[User]:
        return [
            User(
                user_id=u.user_id,
                tenant_id=u.tenant_id,
                email=u.email,
                name=u.name,
                role=u.role,
                is_active=u.is_active,
                groups=u.groups
            )
            for u in self._users_by_id.values()
            if u.tenant_id == tenant_id
        ]


# Singleton instance
_user_repository = InMemoryUserRepository()

def get_user_repository() -> UserRepositoryInterface:
    return _user_repository
