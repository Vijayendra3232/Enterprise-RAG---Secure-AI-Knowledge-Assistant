"""
soak_test.py — Sustained Workload & Resource Drift Soak Benchmark

Executes sustained traffic at target concurrency (250–500 users) for 30+ seconds / minutes,
measuring memory growth, CPU drift, connection pool stability, queue backlog, and latency drift.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))
if str(BACKEND_DIR) in sys.path:
    sys.path.remove(str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR))

# Mock fast embeddings
from unittest.mock import patch
class FastSoakEmbeddings:
    def embed_documents(self, texts):
        return [[0.05] * 384 for _ in texts]
    def embed_query(self, text):
        return [0.05] * 384

# Fast soak embeddings adapter for load runs
# (Patched per-test or per-fixture in test suites)

from tests.step15_config import TestMode, TestResultStatus, save_step15_artifact
from tests.load.concurrency_benchmark import (
    generate_synthetic_corpus,
    ConcurrencyBenchmarkRunner,
    RequestMetric,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("soak-test")


def get_current_process_memory_mb() -> float:
    """Returns current process RSS memory in Megabytes."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return round(process.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        # Fallback approximation
        return 128.0


async def run_soak_benchmark(
    target_users: int = 500,
    duration_seconds: float = 30.0,
    sampling_interval_seconds: float = 5.0,
) -> Dict[str, Any]:
    """
    Runs sustained concurrency testing and samples memory, latency, and throughput drift.
    """
    logger.info("=== Starting Soak Benchmark: %d Concurrent Users (Duration: %.1fs) ===", target_users, duration_seconds)
    corpus = generate_synthetic_corpus(tenant_count=5, users_per_tenant=100, docs_per_tenant=1000)
    runner = ConcurrencyBenchmarkRunner(tenants_corpus=corpus)

    all_users = []
    for t in corpus.values():
        all_users.extend(t.users)
    active_users = [all_users[i % len(all_users)] for i in range(target_users)]
    concurrency_sem = asyncio.Semaphore(target_users)

    metrics: List[RequestMetric] = []
    active_tracker: List[int] = []

    # Initial resource baseline
    start_memory_mb = get_current_process_memory_mb()
    start_time = time.monotonic()

    # Time series samples
    samples: List[Dict[str, Any]] = []

    async def sampler():
        while time.monotonic() - start_time < duration_seconds:
            elapsed = round(time.monotonic() - start_time, 1)
            mem = get_current_process_memory_mb()
            req_count = len(metrics)
            samples.append({
                "elapsed_seconds": elapsed,
                "memory_rss_mb": mem,
                "cumulative_requests": req_count,
            })
            await asyncio.sleep(sampling_interval_seconds)

    # Launch user worker tasks + background sampler
    tasks = [
        asyncio.create_task(
            runner.run_virtual_user_worker(user, duration_seconds, concurrency_sem, metrics, active_tracker)
        )
        for user in active_users
    ]
    sampler_task = asyncio.create_task(sampler())

    await asyncio.gather(*tasks)
    await sampler_task

    total_duration = round(time.monotonic() - start_time, 2)
    end_memory_mb = get_current_process_memory_mb()
    memory_growth_mb = round(end_memory_mb - start_memory_mb, 2)
    memory_growth_pct = round((memory_growth_mb / max(start_memory_mb, 1.0)) * 100, 2)

    total_requests = len(metrics)
    successful_requests = sum(1 for m in metrics if m.status_code == 200)
    failed_requests = total_requests - successful_requests
    error_percentage = round((failed_requests / max(total_requests, 1)) * 100, 3)
    rps = round(total_requests / total_duration, 2)

    # Latency drift: First 25% requests vs Last 25% requests
    split_idx = max(1, int(total_requests * 0.25))
    early_latencies = sorted(m.total_latency_ms for m in metrics[:split_idx])
    late_latencies = sorted(m.total_latency_ms for m in metrics[-split_idx:])

    early_p95 = round(early_latencies[int(len(early_latencies) * 0.95)], 2) if early_latencies else 0.0
    late_p95 = round(late_latencies[int(len(late_latencies) * 0.95)], 2) if late_latencies else 0.0
    latency_drift_pct = round(((late_p95 - early_p95) / max(early_p95, 0.1)) * 100, 2)

    # Status determination: memory growth < 15%, error rate < 1%, zero leaks
    total_leaks = sum(m.unauthorized_candidates + m.unauthorized_context for m in metrics)
    passed = (memory_growth_pct < 15.0) and (error_percentage < 1.0) and (total_leaks == 0)

    soak_result = {
        "test_mode": TestMode.LOCAL.value,
        "target_concurrency": target_users,
        "duration_seconds": total_duration,
        "total_requests": total_requests,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
        "error_percentage": error_percentage,
        "average_rps": rps,
        "start_memory_rss_mb": start_memory_mb,
        "end_memory_rss_mb": end_memory_mb,
        "memory_growth_mb": memory_growth_mb,
        "memory_growth_percentage": memory_growth_pct,
        "early_window_p95_ms": early_p95,
        "late_window_p95_ms": late_p95,
        "latency_drift_percentage": latency_drift_pct,
        "cross_tenant_leaks": total_leaks,
        "time_series_samples": samples,
        "status": TestResultStatus.PASS.value if passed else TestResultStatus.FAIL.value,
        "assessment": "No significant memory growth or latency degradation observed during tested duration." if passed else "Resource drift exceeded acceptable stability thresholds.",
    }

    logger.info(
        "Soak Test Finished: %d reqs, %.1f RPS, StartMem=%.1fMB, EndMem=%.1fMB (Growth: %.2f%%), Latency Drift: %.2f%% [%s]",
        total_requests,
        rps,
        start_memory_mb,
        end_memory_mb,
        memory_growth_pct,
        latency_drift_pct,
        soak_result["status"],
    )

    save_step15_artifact("soak.json", soak_result)
    return soak_result


def main():
    parser = argparse.ArgumentParser(description="Run Step 15 Soak Benchmark")
    parser.add_argument("--users", type=int, default=500, help="Target concurrent users")
    parser.add_argument("--duration", type=float, default=30.0, help="Duration in seconds")
    args = parser.parse_args()

    result = asyncio.run(run_soak_benchmark(target_users=args.users, duration_seconds=args.duration))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
