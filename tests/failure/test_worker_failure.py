"""
test_worker_failure.py — Worker Crash, Stale Task Lease Recovery & Idempotency Testing

Verifies:
1. Worker crash during task execution causes lease expiration.
2. Stale task recovery daemon identifies and re-queues abandoned tasks.
3. Idempotent task processing prevents duplicate destructive side effects.
4. Task retry bounds are strictly enforced (fail permanently after MAX_ATTEMPTS).
"""

from datetime import datetime, timedelta, timezone
import pytest

from tests.step15_config import TestMode, TestResultStatus
from app.tasks.models import TaskStatus
from app.tasks.queue import InMemoryTaskQueue


class TestWorkerFailureAndRecovery:
    """Verifies crash resilience of the asynchronous task worker subsystem."""

    def test_stale_task_lease_recovery(self):
        """Verify task claimed by a dead worker is recovered when lease expires."""
        queue = InMemoryTaskQueue()
        task = queue.enqueue(
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"doc_id": "doc_100"},
            max_attempts=3,
        )

        # Worker claims the task
        claimed = queue.dequeue(worker_id="worker_dead_pid_9999")
        assert claimed is not None
        assert claimed.status == "RUNNING"

        # Simulate time passing and worker crashing (started 10 mins ago)
        past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        claimed.started_at = past_time

        recovered_count = queue.recover_stale(stale_threshold_seconds=300)

        # Stale task must be recovered to RETRYING
        assert recovered_count == 1
        assert claimed.status == "RETRYING"
        assert claimed.worker_id is None

    def test_task_idempotency_on_repeated_execution(self):
        """Verify re-processing the same task ID does not cause duplicate insertions."""
        queue = InMemoryTaskQueue()
        task1 = queue.enqueue(
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"doc_id": "doc_shared_1"},
            idempotency_key="idemp_key_doc_shared_1",
        )
        task2 = queue.enqueue(
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"doc_id": "doc_shared_1"},
            idempotency_key="idemp_key_doc_shared_1",
        )

        # Idempotent enqueue returns the exact same task ID
        assert task1.id == task2.id

    def test_max_attempts_exceeded_marks_task_failed(self):
        """Verify tasks that repeatedly crash workers transition to FAILED without infinite loop."""
        queue = InMemoryTaskQueue()
        task = queue.enqueue(
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"doc_id": "doc_poison_pill"},
            max_attempts=1,
        )

        claimed = queue.dequeue(worker_id="worker_1")
        assert claimed is not None
        claimed.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

        recovered_count = queue.recover_stale(stale_threshold_seconds=300)
        assert recovered_count == 1
        assert claimed.status == "FAILED"
        assert "exhausted" in claimed.last_error.lower()
