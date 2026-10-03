"""
repository.py — User repository abstraction, database-backed SQL persistence, and in-memory fallback implementations.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict
from app.auth.models import User, UserInDB
from app.auth.password import hash_password
from app.storage.database import SessionLocal
from app.storage.models import User as DBUser
from app import config


class UserRepositoryInterface(ABC):
    """
    Abstract repository interface for user identity management.
    Designed for seamless PostgreSQL / SQLAlchemy user persistence.
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


class _UsersByIdDict(dict):
    def __init__(self, repo: "SQLUserRepository"):
        super().__init__()
        self.repo = repo

    def __setitem__(self, key, value: UserInDB):
        super().__setitem__(key, value)
        try:
            self.repo.create(value)
        except ValueError:
            pass

    def get(self, key, default=None):
        user = self.repo.get_by_id(key)
        if user:
            return user
        return super().get(key, default)


class _UsersByEmailDict(dict):
    def __init__(self, repo: "SQLUserRepository"):
        super().__init__()
        self.repo = repo

    def __setitem__(self, key, value: UserInDB):
        super().__setitem__(key.lower(), value)
        try:
            self.repo.create(value)
        except ValueError:
            pass

    def get(self, key, default=None):
        user = self.repo.get_by_email(key)
        if user:
            return user
        return super().get(key.lower() if isinstance(key, str) else key, default)


class SQLUserRepository(UserRepositoryInterface):
    """
    Authoritative database-backed user repository using SQLAlchemy.
    Reads and updates user identity records from PostgreSQL / SQLite metadata database ('users' table).
    """
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    @property
    def _users_by_id(self):
        if not hasattr(self, "_legacy_users_by_id"):
            self._legacy_users_by_id = _UsersByIdDict(self)
        return self._legacy_users_by_id

    @property
    def _users_by_email(self):
        if not hasattr(self, "_legacy_users_by_email"):
            self._legacy_users_by_email = _UsersByEmailDict(self)
        return self._legacy_users_by_email



    def _to_user_in_db(self, db_user: DBUser) -> UserInDB:
        return UserInDB(
            user_id=db_user.user_id,
            tenant_id=db_user.tenant_id,
            email=db_user.email,
            name=db_user.name,
            role=db_user.role,
            is_active=db_user.is_active,
            groups=db_user.groups or [],
            hashed_password=db_user.password_hash,
        )

    def get_by_id(self, user_id: str) -> Optional[UserInDB]:
        db = self.session_factory()
        try:
            db_user = db.query(DBUser).filter(DBUser.user_id == user_id).first()
            return self._to_user_in_db(db_user) if db_user else None
        finally:
            db.close()

    def get_by_email(self, email: str) -> Optional[UserInDB]:
        db = self.session_factory()
        try:
            db_user = db.query(DBUser).filter(DBUser.email.ilike(email.strip())).first()
            return self._to_user_in_db(db_user) if db_user else None
        finally:
            db.close()

    def create(self, user: UserInDB) -> UserInDB:
        db = self.session_factory()
        try:
            existing_id = db.query(DBUser).filter(DBUser.user_id == user.user_id).first()
            if existing_id:
                raise ValueError(f"User with ID '{user.user_id}' already exists.")
            existing_email = db.query(DBUser).filter(DBUser.email.ilike(user.email.strip())).first()
            if existing_email:
                raise ValueError(f"User with email '{user.email}' already exists.")

            db_user = DBUser(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                email=user.email,
                name=user.name,
                password_hash=user.hashed_password,
                role=user.role,
                is_active=user.is_active,
                groups=user.groups or [],
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            return self._to_user_in_db(db_user)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_by_tenant(self, tenant_id: str) -> List[User]:
        db = self.session_factory()
        try:
            db_users = db.query(DBUser).filter(DBUser.tenant_id == tenant_id).all()
            return [
                User(
                    user_id=u.user_id,
                    tenant_id=u.tenant_id,
                    email=u.email,
                    name=u.name,
                    role=u.role,
                    is_active=u.is_active,
                    groups=u.groups or [],
                )
                for u in db_users
            ]
        finally:
            db.close()


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


# Singleton instances
_in_memory_repository = InMemoryUserRepository()
_sql_repository = SQLUserRepository()
_user_repository_override: Optional[UserRepositoryInterface] = None


def set_user_repository(repo: Optional[UserRepositoryInterface]) -> None:
    """
    Override active user repository (useful for testing).
    """
    global _user_repository_override
    _user_repository_override = repo


def get_user_repository() -> UserRepositoryInterface:
    """
    Returns the authoritative user repository.
    By default (or when config.USER_REPOSITORY_TYPE == 'sql'), returns SQLUserRepository.
    When configured to 'in_memory' or 'memory', returns InMemoryUserRepository.
    """
    global _user_repository_override
    if _user_repository_override is not None:
        return _user_repository_override

    repo_type = getattr(config, "USER_REPOSITORY_TYPE", "sql").lower()
    if repo_type in ("in_memory", "memory"):
        return _in_memory_repository
    return _sql_repository

