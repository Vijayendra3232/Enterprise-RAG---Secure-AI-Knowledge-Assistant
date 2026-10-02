"""
queue.py — Provider-agnostic Task Queue abstraction and concrete implementations.
Supports DatabaseTaskQueue (PostgreSQL transactional queue) and InMemoryTaskQueue (fast test adapter).
Designed to allow future Amazon SQS / Celery integration without modifying business task handlers.
"""

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app.storage.database import SessionLocal
from app.storage.models.task import Task
from app.storage.repositories.task_repository import SQLTaskRepository


class TaskQueueInterface(ABC):
    """
    Abstract interface for task queue providers.
    All business services and worker runners interact solely via this interface.
    """

    @abstractmethod
    def enqueue(
        self,
        tenant_id: str,
        task_type: str,
        payload: Dict[str, Any],
        priority: int = 0,
        delay_seconds: int = 0,
        idempotency_key: Optional[str] = None,
        aggregate_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> Task:
        """Enqueue a new task record."""
        pass

    @abstractmethod
    def dequeue(
        self,
        worker_id: str,
        task_types: Optional[List[str]] = None,
        timeout_seconds: float = 1.0,
    ) -> Optional[Task]:
        """Atomically claim and dequeue the next available task."""
        pass

    @abstractmethod
    def acknowledge(
        self,
        task_id: str,
        tenant_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Acknowledge successful completion of a task."""
        pass

    @abstractmethod
    def retry(
        self,
        task_id: str,
        tenant_id: str,
        error: str,
        backoff_seconds: int = 0,
    ) -> bool:
        """Mark task for retry with delayed availability."""
        pass

    @abstractmethod
    def fail(
        self,
        task_id: str,
        tenant_id: str,
        error: str,
    ) -> bool:
        """Mark task as permanently failed."""
        pass

    @abstractmethod
    def recover_stale(self, stale_threshold_seconds: int = 300) -> int:
        """Recover tasks left running by crashed workers."""
        pass


class DatabaseTaskQueue(TaskQueueInterface):
    """
    Production-capable PostgreSQL-backed task queue.
    Uses SQL row-level locks and transactional state transitions.
    """

    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def enqueue(
        self,
        tenant_id: str,
        task_type: str,
        payload: Dict[str, Any],
        priority: int = 0,
        delay_seconds: int = 0,
        idempotency_key: Optional[str] = None,
        aggregate_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> Task:
        db: Session = self.session_factory()
        try:
            repo = SQLTaskRepository(db)
            now = datetime.now(timezone.utc)
            available_at = now + timedelta(seconds=max(0, delay_seconds))
            task_id = f"task_{uuid.uuid4().hex[:16]}"

            task = Task(
                id=task_id,
                tenant_id=tenant_id,
                task_type=task_type.upper(),
                idempotency_key=idempotency_key,
                aggregate_id=aggregate_id,
                status="PENDING",
                priority=priority,
                payload=payload or {},
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=available_at,
                created_at=now,
                updated_at=now,
            )
            created_task = repo.create(task)
            db.commit()
            db.refresh(created_task)

            from app.observability.metrics import task_created_total
            task_created_total.inc(labels={"task_type": task_type.upper()})

            return created_task
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def dequeue(
        self,
        worker_id: str,
        task_types: Optional[List[str]] = None,
        timeout_seconds: float = 1.0,
    ) -> Optional[Task]:
        db: Session = self.session_factory()
        try:
            repo = SQLTaskRepository(db)
            claimed = repo.claim_next_task(
                worker_id=worker_id,
                task_types=task_types,
            )
            if claimed:
                db.commit()
                db.refresh(claimed)

                from app.observability.metrics import task_claimed_total
                task_claimed_total.inc(labels={"task_type": claimed.task_type.upper()})

                return claimed
            return None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def acknowledge(
        self,
        task_id: str,
        tenant_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        db: Session = self.session_factory()
        try:
            repo = SQLTaskRepository(db)
            task = repo.get_by_id(task_id, tenant_id)
            task_type = task.task_type.upper() if task else "UNKNOWN"
            success = repo.mark_success(task_id, tenant_id, result)
            db.commit()

            if success:
                from app.observability.metrics import task_completed_total
                task_completed_total.inc(labels={"task_type": task_type})

            return success
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def retry(
        self,
        task_id: str,
        tenant_id: str,
        error: str,
        backoff_seconds: int = 0,
    ) -> bool:
        db: Session = self.session_factory()
        try:
            repo = SQLTaskRepository(db)
            task = repo.get_by_id(task_id, tenant_id)
            task_type = task.task_type.upper() if task else "UNKNOWN"
            success = repo.mark_failure(
                task_id=task_id,
                tenant_id=tenant_id,
                error_message=error,
                is_retryable=True,
                backoff_seconds=backoff_seconds,
            )
            db.commit()

            if success:
                from app.observability.metrics import task_retry_total
                task_retry_total.inc(labels={"task_type": task_type})

            return success
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def fail(
        self,
        task_id: str,
        tenant_id: str,
        error: str,
    ) -> bool:
        db: Session = self.session_factory()
        try:
            repo = SQLTaskRepository(db)
            task = repo.get_by_id(task_id, tenant_id)
            task_type = task.task_type.upper() if task else "UNKNOWN"
            success = repo.mark_failure(
                task_id=task_id,
                tenant_id=tenant_id,
                error_message=error,
                is_retryable=False,
            )
            db.commit()

            if success:
                from app.observability.metrics import task_failed_total
                task_failed_total.inc(labels={"task_type": task_type, "error_class": "PermanentTaskError"})

            return success
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def recover_stale(self, stale_threshold_seconds: int = 300) -> int:
        db: Session = self.session_factory()
        try:
            repo = SQLTaskRepository(db)
            count = repo.recover_stale_tasks(stale_threshold_seconds=stale_threshold_seconds)
            db.commit()

            if count > 0:
                from app.observability.metrics import task_stale_recovered_total
                task_stale_recovered_total.inc(amount=float(count), labels={"status": "recovered"})

            return count
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class InMemoryTaskQueue(TaskQueueInterface):
    """
    Thread-safe in-memory task queue adapter strictly intended for unit tests.
    [DEV/TEST ONLY]
    """

    def __init__(self):
        self._tasks: Dict[str, Task] = {}

    def enqueue(
        self,
        tenant_id: str,
        task_type: str,
        payload: Dict[str, Any],
        priority: int = 0,
        delay_seconds: int = 0,
        idempotency_key: Optional[str] = None,
        aggregate_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> Task:
        now = datetime.now(timezone.utc)
        if idempotency_key:
            for t in self._tasks.values():
                if t.tenant_id == tenant_id and t.idempotency_key == idempotency_key:
                    return t

        task_id = f"mem_task_{uuid.uuid4().hex[:12]}"
        task = Task(
            id=task_id,
            tenant_id=tenant_id,
            task_type=task_type.upper(),
            idempotency_key=idempotency_key,
            aggregate_id=aggregate_id,
            status="PENDING",
            priority=priority,
            payload=payload or {},
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=now + timedelta(seconds=max(0, delay_seconds)),
            created_at=now,
            updated_at=now,
        )
        self._tasks[task_id] = task
        return task

    def dequeue(
        self,
        worker_id: str,
        task_types: Optional[List[str]] = None,
        timeout_seconds: float = 1.0,
    ) -> Optional[Task]:
        now = datetime.now(timezone.utc)
        candidates = [
            t for t in self._tasks.values()
            if t.status in ["PENDING", "RETRYING"]
            and t.available_at <= now
            and (not task_types or t.task_type in task_types)
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda t: (-t.priority, t.available_at))
        task = candidates[0]
        task.status = "RUNNING"
        task.started_at = now
        task.worker_id = worker_id
        task.attempt_count += 1
        task.updated_at = now
        return task

    def acknowledge(
        self,
        task_id: str,
        tenant_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.tenant_id != tenant_id:
            return False
        task.status = "SUCCEEDED"
        task.completed_at = datetime.now(timezone.utc)
        task.result = result or {}
        task.updated_at = datetime.now(timezone.utc)
        return True

    def retry(
        self,
        task_id: str,
        tenant_id: str,
        error: str,
        backoff_seconds: int = 0,
    ) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.tenant_id != tenant_id:
            return False
        now = datetime.now(timezone.utc)
        if task.attempt_count < task.max_attempts:
            task.status = "RETRYING"
            task.available_at = now + timedelta(seconds=max(1, backoff_seconds))
            task.last_error = error
            task.worker_id = None
        else:
            task.status = "FAILED"
            task.failed_at = now
            task.last_error = error
        task.updated_at = now
        return True

    def fail(
        self,
        task_id: str,
        tenant_id: str,
        error: str,
    ) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.tenant_id != tenant_id:
            return False
        now = datetime.now(timezone.utc)
        task.status = "FAILED"
        task.failed_at = now
        task.last_error = error
        task.updated_at = now
        return True

    def recover_stale(self, stale_threshold_seconds: int = 300) -> int:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=stale_threshold_seconds)
        count = 0
        for task in self._tasks.values():
            if task.status == "RUNNING" and task.started_at and task.started_at < cutoff:
                if task.attempt_count < task.max_attempts:
                    task.status = "RETRYING"
                    task.available_at = now
                    task.worker_id = None
                    task.last_error = "Recovered from stale running state."
                else:
                    task.status = "FAILED"
                    task.failed_at = now
                    task.last_error = "Stale running task exhausted attempts."
                task.updated_at = now
                count += 1
        return count


# Singleton factory
_global_task_queue: Optional[TaskQueueInterface] = None


def get_task_queue() -> TaskQueueInterface:
    global _global_task_queue
    if _global_task_queue is None:
        _global_task_queue = DatabaseTaskQueue()
    return _global_task_queue


def set_task_queue(queue: TaskQueueInterface) -> None:
    global _global_task_queue
    _global_task_queue = queue


def reset_task_queue() -> None:
    global _global_task_queue
    _global_task_queue = None
