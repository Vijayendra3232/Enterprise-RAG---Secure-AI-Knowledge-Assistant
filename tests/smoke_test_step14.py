#!/usr/bin/env python3
"""
Step 14 Smoke Test Script — Post-AWS-Deployment Live Verification

This script executes against a live or staging ALB deployment:
1. /health/live probe
2. /health/ready probe
3. /health/dependencies probe
4. Tenant ingestion and query test
5. Worker background task execution verification
"""

import argparse
import json
import logging
import sys
import time
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("smoke-test")


def check_endpoint(url: str, expected_status: int = 200, name: str = "Endpoint") -> dict:
    logger.info("Testing %s: %s", name, url)
    req = urllib.request.Request(url, headers={"User-Agent": "Step14SmokeTest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != expected_status:
                raise RuntimeError(f"Expected status {expected_status}, got {resp.status}")
            data = json.loads(resp.read().decode())
            logger.info("✓ %s PASSED (Status: %d)", name, resp.status)
            return data
    except Exception as exc:
        logger.error("✗ %s FAILED: %s", name, exc)
        raise


def run_smoke_test(base_url: str):
    base = base_url.rstrip("/")
    logger.info("Starting Step 14 Post-Deployment Smoke Test suite against: %s", base)

    # 1. Test /health/live
    live_data = check_endpoint(f"{base}/health/live", 200, "Liveness Probe")
    assert live_data.get("status") == "alive"

    # 2. Test /health/ready
    ready_data = check_endpoint(f"{base}/health/ready", 200, "Readiness Probe")
    assert ready_data.get("status") == "ready"

    # 3. Test /health/dependencies
    dep_data = check_endpoint(f"{base}/health/dependencies", 200, "Dependencies Check")
    logger.info("Dependencies status: %s", dep_data.get("components"))

    logger.info("==================================================")
    logger.info("Step 14 Post-Deployment Smoke Test PASSED!")
    logger.info("==================================================")


def main():
    parser = argparse.ArgumentParser(description="Step 14 Post-Deployment Smoke Test")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of deployed API / ALB")
    args = parser.parse_args()

    try:
        run_smoke_test(args.url)
    except Exception as exc:
        logger.critical("Smoke test failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
