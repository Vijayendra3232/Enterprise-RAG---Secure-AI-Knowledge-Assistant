"""
database.py — SQLAlchemy 2.x database engine, session factory, and connection pool configuration.
Supports PostgreSQL for production and SQLite with thread safety for dev/testing.
"""

import os
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.pool import StaticPool, QueuePool

from app import config

# Ensure data directory exists for local database paths
if config.DATABASE_URL.startswith("sqlite"):
    db_path = config.DATABASE_URL.replace("sqlite:///", "")
    if db_path and not db_path.startswith(":memory:"):
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
            except Exception as e:
                print(f"[Database] Warning: Could not create directory {db_dir}: {e}")

# Build engine parameters based on dialect
engine_kwargs = {}
if config.DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    if ":memory:" in config.DATABASE_URL:
        engine_kwargs["poolclass"] = StaticPool
else:
    # Production PostgreSQL connection pool settings
    engine_kwargs.update({
        "poolclass": QueuePool,
        "pool_size": config.DB_POOL_SIZE,
        "max_overflow": config.DB_MAX_OVERFLOW,
        "pool_timeout": config.DB_POOL_TIMEOUT,
        "pool_recycle": config.DB_POOL_RECYCLE,
        "pool_pre_ping": config.DB_POOL_PRE_PING,
    })

engine = create_engine(config.DATABASE_URL, **engine_kwargs)

# ─── Database Telemetry & Event Listeners ────────────────────────────────────
import time
from sqlalchemy import event

@event.listens_for(engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if context:
        context._query_start_time = time.perf_counter()

@event.listens_for(engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if context and hasattr(context, "_query_start_time"):
        duration_ms = (time.perf_counter() - context._query_start_time) * 1000.0
        try:
            from app.observability.metrics import db_queries_total, db_query_duration_seconds, db_slow_queries_total
            db_queries_total.inc(labels={"operation": "sql_query", "status": "success"})
            db_query_duration_seconds.observe(duration_ms / 1000.0, labels={"operation": "sql_query"})

            slow_threshold = int(getattr(config, "SLOW_DB_THRESHOLD_MS", 200))
            if duration_ms > slow_threshold:
                db_slow_queries_total.inc(labels={"operation": "sql_query"})
        except Exception:
            pass

@event.listens_for(engine, "handle_error")
def _handle_db_error(exception_context):
    try:
        from app.observability.metrics import db_queries_total
        db_queries_total.inc(labels={"operation": "sql_query", "status": "error"})
    except Exception:
        pass

@event.listens_for(engine.pool, "checkout")
def _pool_checkout(dbapi_connection, connection_record, connection_proxy):
    try:
        from app.observability.metrics import db_pool_checked_out_gauge
        db_pool_checked_out_gauge.inc(labels={"pool_name": "main_pool"})
    except Exception:
        pass

@event.listens_for(engine.pool, "checkin")
def _pool_checkin(dbapi_connection, connection_record):
    try:
        from app.observability.metrics import db_pool_checked_out_gauge
        db_pool_checked_out_gauge.dec(labels={"pool_name": "main_pool"})
    except Exception:
        pass

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding an isolated database session per request.
    Guarantees session cleanup upon completion or failure.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """
    Context manager for transactional database operations outside FastAPI requests.
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(target_engine=None) -> None:
    """
    Create all metadata tables. Typically called during app startup or test fixtures.
    """
    # Import all models to ensure they are registered with Base.metadata
    import app.storage.models  # noqa: F401
    Base.metadata.create_all(bind=target_engine or engine)
