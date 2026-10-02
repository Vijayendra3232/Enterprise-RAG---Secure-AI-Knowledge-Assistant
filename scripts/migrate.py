#!/usr/bin/env python3
"""
Enterprise RAG - Database Migration Runner

Executes Alembic migrations forward (upgrade head) in a controlled manner.
"""

import argparse
import logging
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("migration-runner")


def run_migrations(target_revision: str = "head") -> int:
    """Run Alembic upgrade to the given revision."""
    logger.info("Executing database migration to revision '%s'...", target_revision)
    backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")

    cmd = [sys.executable, "-m", "alembic", "upgrade", target_revision]
    logger.info("Running command: %s (cwd=%s)", " ".join(cmd), backend_dir)

    result = subprocess.run(cmd, cwd=backend_dir)
    if result.returncode == 0:
        logger.info("Migrations successfully applied.")
    else:
        logger.error("Migration failed with return code %d.", result.returncode)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Run Alembic Database Migrations")
    parser.add_argument("--revision", default="head", help="Target revision (default: head)")
    args = parser.parse_args()

    rc = run_migrations(args.revision)
    sys.exit(rc)


if __name__ == "__main__":
    main()
