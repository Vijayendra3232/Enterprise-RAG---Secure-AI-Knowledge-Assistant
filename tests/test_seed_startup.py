"""
test_seed_startup.py — Tests for database seeding & AUTO_SEED_DATA startup hook.
"""

import os
import sys
import pytest
from unittest.mock import patch

# Ensure backend directory is in path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from scripts.seed_dev import seed_database
from app.storage.database import SessionLocal, init_db
from app.storage.models import User as DBUser, Tenant


def test_seed_database_execution_and_idempotency():
    """Verify seed_database populates users and tenants idempotently."""
    # First execution
    seed_database()

    db = SessionLocal()
    try:
        admin_user = db.query(DBUser).filter(DBUser.user_id == "admin_a").first()
        assert admin_user is not None
        assert admin_user.email == "admin@companya.com"
        assert admin_user.role == "ADMIN"

        tenant_a = db.query(Tenant).filter(Tenant.tenant_id == "company_a").first()
        assert tenant_a is not None
    finally:
        db.close()

    # Second execution (must succeed without primary key conflict)
    seed_database()


def test_seed_database_custom_env_passwords(monkeypatch):
    """Verify seed_database uses custom password environment variables."""
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "custom_admin_pass_99")
    seed_database()

    db = SessionLocal()
    try:
        from app.auth.password import verify_password
        admin_user = db.query(DBUser).filter(DBUser.user_id == "admin_a").first()
        assert admin_user is not None
        assert verify_password("custom_admin_pass_99", admin_user.password_hash)
    finally:
        db.close()


import asyncio


def test_lifespan_startup_seeding_hook(monkeypatch):
    """Verify FastAPI lifespan triggers seeding when AUTO_SEED_DATA=true."""
    from app import config
    from app.main import lifespan, app

    monkeypatch.setattr(config, "AUTO_SEED_DATA", True)

    async def _test():
        async with lifespan(app):
            # Verify app is alive and seeded
            db = SessionLocal()
            try:
                admin_user = db.query(DBUser).filter(DBUser.user_id == "admin_a").first()
                assert admin_user is not None
            finally:
                db.close()

    asyncio.run(_test())


def test_lifespan_startup_seeding_error_resilience(monkeypatch):
    """Verify application startup continues cleanly even if seeding fails."""
    from app import config
    from app.main import lifespan, app

    monkeypatch.setattr(config, "AUTO_SEED_DATA", True)

    async def _test():
        with patch("scripts.seed_dev.seed_database", side_effect=RuntimeError("DB seed error simulate")):
            # Lifespan should catch error and continue startup
            async with lifespan(app):
                pass  # Should reach here without raising exception

    asyncio.run(_test())

