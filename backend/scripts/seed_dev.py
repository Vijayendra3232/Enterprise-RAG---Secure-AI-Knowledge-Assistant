"""
seed_dev.py — Development database seeder.
Populates standard tenants and test users with securely hashed passwords.
"""

import os
import sys

# Ensure backend directory is in python path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.storage.database import init_db, SessionLocal
from app.storage.models import Tenant, User as DBUser
from app.auth.password import hash_password


def seed_database():
    print("[Seed] Initializing database schema...")
    init_db()

    db = SessionLocal()
    try:
        # 1. Seed Tenants
        tenants = [
            Tenant(tenant_id="company_a", name="Company A (Corp)", is_active=True),
            Tenant(tenant_id="company_b", name="Company B (Partner)", is_active=True),
            Tenant(tenant_id="default_tenant", name="Default Tenant", is_active=True),
        ]
        for t in tenants:
            existing_t = db.query(Tenant).filter(Tenant.tenant_id == t.tenant_id).first()
            if not existing_t:
                db.add(t)
                print(f"  + Added tenant: {t.tenant_id}")
            else:
                print(f"  * Tenant already exists: {t.tenant_id}")
        db.flush()

        # 2. Seed Users (passwords supplied via environment variables with test fallbacks)
        admin_pw = os.getenv("DEMO_ADMIN_PASSWORD", "admin123")
        manager_pw = os.getenv("DEMO_MANAGER_PASSWORD", "manager123")
        user_pw = os.getenv("DEMO_USER_PASSWORD", "password123")
        viewer_pw = os.getenv("DEMO_VIEWER_PASSWORD", "viewer123")

        users = [
            DBUser(
                user_id="admin_a",
                tenant_id="company_a",
                email="admin@companya.com",
                name="Company A Admin",
                role="ADMIN",
                is_active=True,
                groups=["executives", "admins"],
                password_hash=hash_password(admin_pw),
            ),
            DBUser(
                user_id="manager_a",
                tenant_id="company_a",
                email="manager@companya.com",
                name="Company A Manager",
                role="MANAGER",
                is_active=True,
                groups=["management"],
                password_hash=hash_password(manager_pw),
            ),
            DBUser(
                user_id="user_a",
                tenant_id="company_a",
                email="usera@companya.com",
                name="User A (Engineering)",
                role="ENGINEERING",
                is_active=True,
                groups=["engineering", "dev"],
                password_hash=hash_password(user_pw),
            ),
            DBUser(
                user_id="user_b",
                tenant_id="company_a",
                email="userb@companya.com",
                name="User B (Finance)",
                role="FINANCE",
                is_active=True,
                groups=["finance", "accounting"],
                password_hash=hash_password(user_pw),
            ),
            DBUser(
                user_id="viewer_a",
                tenant_id="company_a",
                email="viewer@companya.com",
                name="Company A Viewer",
                role="VIEWER",
                is_active=True,
                groups=["guests"],
                password_hash=hash_password(viewer_pw),
            ),
            DBUser(
                user_id="admin_b",
                tenant_id="company_b",
                email="admin@companyb.com",
                name="Company B Admin",
                role="ADMIN",
                is_active=True,
                groups=["admins"],
                password_hash=hash_password(admin_pw),
            ),
            DBUser(
                user_id="user_c",
                tenant_id="company_b",
                email="userc@companyb.com",
                name="User C (Company B Eng)",
                role="ENGINEERING",
                is_active=True,
                groups=["engineering"],
                password_hash=hash_password(user_pw),
            ),
            DBUser(
                user_id="inactive_user",
                tenant_id="company_a",
                email="inactive@companya.com",
                name="Inactive User",
                role="USER",
                is_active=False,
                groups=[],
                password_hash=hash_password("inactive123"),
            ),
        ]

        for u in users:
            existing_u = db.query(DBUser).filter(DBUser.user_id == u.user_id).first()
            if not existing_u:
                db.add(u)
                print(f"  + Added user: {u.user_id} ({u.email})")
            else:
                existing_u.email = u.email
                existing_u.name = u.name
                existing_u.role = u.role
                existing_u.is_active = u.is_active
                existing_u.password_hash = u.password_hash
                existing_u.groups = u.groups
                print(f"  * Updated user: {u.user_id}")

        db.commit()
        print("[Seed] Seeding completed successfully!")
    except Exception as e:
        db.rollback()
        print(f"[Seed] Error seeding database: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
