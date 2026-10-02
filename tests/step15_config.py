"""
step15_config.py — Central Test Configuration & Result Classification Framework

Defines explicit test modes and result status reporting for Step 15 Validation.
Every test must declare its operational mode and produce standard status outputs:
- TestMode: LOCAL, SIMULATED, AWS_MOCKED, AWS_LIVE, BLOCKED, NOT_TESTED
- TestResultStatus: PASS, FAIL, BLOCKED, NOT_TESTED
"""

import os
import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "step15"


class TestMode(str, Enum):
    __test__ = False
    LOCAL = "LOCAL"
    SIMULATED = "SIMULATED"
    AWS_MOCKED = "AWS_MOCKED"
    AWS_LIVE = "AWS_LIVE"
    BLOCKED = "BLOCKED"
    NOT_TESTED = "NOT_TESTED"


class TestResultStatus(str, Enum):
    __test__ = False
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_TESTED = "NOT_TESTED"


# Central environment configuration flags
STEP15_TEST_MODE = os.getenv("STEP15_TEST_MODE", TestMode.LOCAL.value)
RUN_LIVE_AWS_TESTS = os.getenv("RUN_LIVE_AWS_TESTS", "false").lower() == "true"
RUN_LIVE_LLM_TESTS = os.getenv("RUN_LIVE_LLM_TESTS", "false").lower() == "true"


def ensure_artifacts_dir() -> Path:
    """Ensure artifacts/step15 directory exists for machine-readable evidence."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


def save_step15_artifact(filename: str, data: Dict[str, Any]) -> Path:
    """Save machine-readable evidence to artifacts/step15/."""
    out_dir = ensure_artifacts_dir()
    file_path = out_dir / filename
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return file_path


def get_live_aws_status(feature_name: str) -> Dict[str, Any]:
    """Check whether live AWS environment is available or gated."""
    if not RUN_LIVE_AWS_TESTS:
        return {
            "feature": feature_name,
            "mode": TestMode.BLOCKED.value,
            "status": TestResultStatus.BLOCKED.value,
            "reason": "BLOCKED — LIVE AWS ENVIRONMENT NOT AVAILABLE (RUN_LIVE_AWS_TESTS=false)",
        }
    return {
        "feature": feature_name,
        "mode": TestMode.AWS_LIVE.value,
        "status": TestResultStatus.PASS.value,
        "reason": "Live AWS environment available",
    }
