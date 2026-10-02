"""
capacity_report.py — Tabulates Step 15 Capacity Benchmarks into Markdown Tables & JSON Summaries.

Reads machine-readable raw evidence from artifacts/step15/ and generates structured tables.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_DIR = ROOT_DIR / "artifacts" / "step15"


def load_artifact(filename: str) -> Optional[Dict[str, Any]]:
    path = ARTIFACTS_DIR / filename
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def generate_capacity_table() -> Tuple[str, Dict[str, Any]]:
    tiers = [25, 100, 250, 500, 750]
    rows = []
    summary_data = {}

    for t in tiers:
        data = load_artifact(f"concurrency_{t}.json")
        if data:
            rps = data.get("rps", 0.0)
            p50 = data.get("latency_p50_ms", 0.0)
            p95 = data.get("latency_p95_ms", 0.0)
            p99 = data.get("latency_p99_ms", 0.0)
            err = data.get("error_percentage", 0.0)
            queue = data.get("max_queue_depth", 0)
            llm_429 = data.get("llm_throttle_429_count", 0)
            status = data.get("status", "NOT_TESTED")

            rows.append(
                f"| {t:11d} | {rps:7.1f} | {p50:7.1f} | {p95:7.1f} | {p99:7.1f} | {err:7.2f}% | {queue:9d} | {llm_429:12d} | {status:9s} |"
            )
            summary_data[f"tier_{t}"] = data
        else:
            rows.append(f"| {t:11d} |     N/A |     N/A |     N/A |     N/A |      N/A |       N/A |          N/A | NOT_TESTED|")

    # Breaking point tier if present
    bp = load_artifact("breaking_point.json")
    if bp:
        t = bp.get("target_concurrency", 1000)
        rps = bp.get("rps", 0.0)
        p50 = bp.get("latency_p50_ms", 0.0)
        p95 = bp.get("latency_p95_ms", 0.0)
        p99 = bp.get("latency_p99_ms", 0.0)
        err = bp.get("error_percentage", 0.0)
        status = bp.get("status", "PASS")
        rows.append(
            f"| {t:11d} | {rps:7.1f} | {p50:7.1f} | {p95:7.1f} | {p99:7.1f} | {err:7.2f}% |         0 |            0 | {status:9s} |"
        )
        summary_data["breaking_point"] = bp

    header = (
        "| Concurrency |     RPS |     p50 |     p95 |     p99 |   Error % | Max Queue | LLM Throttle | Result    |\n"
        "|------------:|--------:|--------:|--------:|--------:|----------:|----------:|-------------:|-----------|"
    )
    table_md = header + "\n" + "\n".join(rows)
    return table_md, summary_data


def main():
    table, summary = generate_capacity_table()
    print("=== STEP 15 MEASURED CAPACITY TABLE ===")
    print(table)


if __name__ == "__main__":
    from typing import Optional, Tuple
    main()
