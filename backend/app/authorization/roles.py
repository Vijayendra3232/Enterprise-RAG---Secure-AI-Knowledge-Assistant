"""
roles.py — Role definitions for enterprise RBAC.
"""

from enum import Enum


class Role(str, Enum):
    """
    Standard enterprise roles and domain roles.
    """
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    USER = "USER"
    VIEWER = "VIEWER"
    
    # Departmental / Domain roles (extensible)
    ENGINEERING = "ENGINEERING"
    FINANCE = "FINANCE"
    HR = "HR"
    LEGAL = "LEGAL"

    @classmethod
    def from_str(cls, value: str) -> "Role":
        try:
            return cls(value.upper())
        except ValueError:
            return cls.USER
