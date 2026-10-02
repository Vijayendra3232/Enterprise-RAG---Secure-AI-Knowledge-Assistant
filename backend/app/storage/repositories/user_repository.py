"""
user_repository.py — User repository implementation backed by SQLAlchemy.
Maintains full compatibility with UserRepositoryInterface and UserInDB models.
"""

from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.auth.models import User, UserInDB
from app.auth.repository import UserRepositoryInterface
from app.storage.models.user import User as DBUser
from app.storage.repositories.base import DuplicateEntityException, EntityNotFoundException


class SQLUserRepository(UserRepositoryInterface):
    """
    SQLAlchemy-backed user repository for production persistent storage.
    """
    def __init__(self, db: Session):
        self.db = db

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
        db_user = self.db.query(DBUser).filter(DBUser.user_id == user_id).first()
        if not db_user:
            return None
        return self._to_user_in_db(db_user)

    def get_by_email(self, email: str, tenant_id: Optional[str] = None) -> Optional[UserInDB]:
        query = self.db.query(DBUser).filter(DBUser.email == email.lower())
        if tenant_id:
            query = query.filter(DBUser.tenant_id == tenant_id)
        db_user = query.first()
        if not db_user:
            return None
        return self._to_user_in_db(db_user)

    def create(self, user: UserInDB) -> UserInDB:
        existing_id = self.get_by_id(user.user_id)
        if existing_id:
            raise DuplicateEntityException(f"User with ID '{user.user_id}' already exists.")

        existing_email = self.get_by_email(user.email, tenant_id=user.tenant_id)
        if existing_email:
            raise DuplicateEntityException(f"User with email '{user.email}' already exists in tenant '{user.tenant_id}'.")

        db_user = DBUser(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            email=user.email.lower(),
            name=user.name,
            password_hash=user.hashed_password,
            role=user.role,
            is_active=user.is_active,
            groups=user.groups,
        )
        try:
            self.db.add(db_user)
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise DuplicateEntityException(f"User constraint violation: {exc}") from exc

        return user

    def update(self, user: UserInDB) -> UserInDB:
        db_user = self.db.query(DBUser).filter(DBUser.user_id == user.user_id).first()
        if not db_user:
            raise EntityNotFoundException(f"User with ID '{user.user_id}' not found.")

        db_user.email = user.email.lower()
        db_user.name = user.name
        db_user.password_hash = user.hashed_password
        db_user.role = user.role
        db_user.is_active = user.is_active
        db_user.groups = user.groups
        self.db.flush()
        return user

    def delete(self, user_id: str) -> bool:
        db_user = self.db.query(DBUser).filter(DBUser.user_id == user_id).first()
        if not db_user:
            return False
        self.db.delete(db_user)
        self.db.flush()
        return True

    def list_by_tenant(self, tenant_id: str) -> List[User]:
        db_users = self.db.query(DBUser).filter(DBUser.tenant_id == tenant_id).all()
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

    def count(self, tenant_id: Optional[str] = None) -> int:
        query = self.db.query(DBUser)
        if tenant_id:
            query = query.filter(DBUser.tenant_id == tenant_id)
        return query.count()
