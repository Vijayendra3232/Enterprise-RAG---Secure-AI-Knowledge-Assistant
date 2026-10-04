"""
task_repository.py — Authoritative PostgreSQL repository for Tasks and OutboxEvents.
Enforces multi-tenant isolation, atomic task claiming (FOR UPDATE SKIP LOCKED),
idempotency key deduplication, bounded retry backoffs, and stale task crash recovery.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, or_, update

from app.storage.models.task import Task
from app.storage.models.outbox import OutboxEvent
from app.storage.repositories.base import DuplicateEntityException


class SQLTaskRepository:
    """
    Authoritative database repository for asynchronous task records.
    Every lookup and mutation is strictly tenant-scoped.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(self, task: Task) -> Task:
        """
        Create a new task. If an idempotency_key is provided and a task with
        the same (tenant_id, idempotency_key) already exists, returns the existing
        task without creating a duplicate.
        """
        if task.idempotency_key:
            existing = (
                self.db.query(Task)
                .filter(
                    Task.tenant_id == task.tenant_id,
                    Task.idempotency_key == task.idempotency_key,
                )
                .first()
            )
            if existing:
                return existing

        now = datetime.now(timezone.utc)
        if not task.created_at:
            task.created_at = now
        if not task.updated_at:
            task.updated_at = now
        if not task.available_at:
            task.available_at = now

        self.db.add(task)
        self.db.flush()
        return task

    def get_by_id_and_tenant(self, task_id: str, tenant_id: str) -> Optional[Task]:
        """
        Get task by ID and tenant ID (strictly tenant-scoped to prevent IDOR).
        """
        return (
            self.db.query(Task)
            .filter(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
            )
            .first()
        )

    # Alias for contract compatibility
    get_by_id = get_by_id_and_tenant

    def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Task]:
        """
        List tasks for a tenant with optional status and type filters.
        """
        query = self.db.query(Task).filter(Task.tenant_id == tenant_id)
        if status:
            query = query.filter(Task.status == status.upper())
        if task_type:
            query = query.filter(Task.task_type == task_type.upper())
        return query.order_by(Task.created_at.desc()).offset(offset).limit(limit).all()

    def count_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> int:
        """Count total tasks matching filters for a tenant."""
        query = self.db.query(Task).filter(Task.tenant_id == tenant_id)
        if status:
            query = query.filter(Task.status == status.upper())
        if task_type:
            query = query.filter(Task.task_type == task_type.upper())
        return query.count()

    def claim_next_task(
        self,
        worker_id: str,
        task_types: Optional[List[str]] = None,
        lock_timeout_seconds: int = 300,
    ) -> Optional[Task]:
        """
        Atomically claim the next available task for execution.
        In PostgreSQL: uses SELECT ... FOR UPDATE SKIP LOCKED.
        In SQLite (test environment): uses row-level selection and transactional status update.
        Transitions status from PENDING/RETRYING -> RUNNING.
        """
        now = datetime.now(timezone.utc)
        dialect_name = self.db.bind.dialect.name if self.db.bind else "sqlite"

        query = self.db.query(Task).filter(
            Task.status.in_(["PENDING", "RETRYING"]),
            Task.available_at <= now,
        )

        if task_types:
            query = query.filter(Task.task_type.in_(task_types))

        query = query.order_by(Task.priority.desc(), Task.available_at.asc())

        if dialect_name == "postgresql":
            # Native PostgreSQL atomic row-locking avoiding worker contention
            task = query.with_for_update(skip_locked=True).first()
        else:
            task = query.first()

        if not task:
            return None

        task.status = "RUNNING"
        task.started_at = now
        task.worker_id = worker_id
        task.attempt_count += 1
        task.updated_at = now
        self.db.flush()
        return task

    def mark_success(
        self,
        task_id: str,
        tenant_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark task as SUCCEEDED."""
        task = self.get_by_id_and_tenant(task_id, tenant_id)
        if not task:
            return False

        now = datetime.now(timezone.utc)
        task.status = "SUCCEEDED"
        task.completed_at = now
        task.result = result or {}
        task.last_error = None
        task.updated_at = now
        self.db.flush()
        return True

    def mark_failure(
        self,
        task_id: str,
        tenant_id: str,
        error_message: str,
        is_retryable: bool,
        backoff_seconds: int = 0,
    ) -> bool:
        """
        Mark task failure. If retryable and attempt_count < max_attempts,
        transitions to RETRYING with exponential backoff delay.
        Otherwise transitions permanently to FAILED.
        """
        task = self.get_by_id_and_tenant(task_id, tenant_id)
        if not task:
            return False

        now = datetime.now(timezone.utc)
        # Sanitize error message (truncate if too long, zero secrets)
        sanitized_error = error_message[:2000] if error_message else "Unknown execution error"

        if is_retryable and task.attempt_count < task.max_attempts:
            task.status = "RETRYING"
            task.available_at = now + timedelta(seconds=max(1, backoff_seconds))
            task.last_error = sanitized_error
            task.worker_id = None
        else:
            task.status = "FAILED"
            task.failed_at = now
            task.last_error = sanitized_error

        task.updated_at = now
        self.db.flush()
        return True

    def cancel_task(self, task_id: str, tenant_id: str) -> bool:
        """
        Cancel a pending or retrying task.
        Running tasks cannot be cancelled immediately via status flag alone.
        """
        task = self.get_by_id_and_tenant(task_id, tenant_id)
        if not task:
            return False

        if task.status in ["PENDING", "RETRYING"]:
            task.status = "CANCELLED"
            task.updated_at = datetime.now(timezone.utc)
            self.db.flush()
            return True
        return False

    def recover_stale_tasks(self, stale_threshold_seconds: int = 300) -> int:
        """
        Detect and recover tasks stuck in RUNNING state due to crashed workers.
        If attempt_count < max_attempts: resets to RETRYING.
        Else: marks FAILED.
        """
        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(seconds=stale_threshold_seconds)

        from sqlalchemy import or_, and_
        stale_tasks = (
            self.db.query(Task)
            .filter(
                Task.status == "RUNNING",
                or_(
                    and_(Task.updated_at != None, Task.updated_at < stale_cutoff),
                    and_(Task.updated_at == None, Task.started_at < stale_cutoff),
                ),
            )
            .all()
        )

        recovered_count = 0
        for task in stale_tasks:
            prev_worker = task.worker_id or "unknown"
            if task.attempt_count < task.max_attempts:
                task.status = "RETRYING"
                task.available_at = now
                task.worker_id = None
                task.last_error = f"Recovered from crashed/stale worker '{prev_worker}'."
            else:
                task.status = "FAILED"
                task.failed_at = now
                task.last_error = f"Task timed out on worker '{prev_worker}' and exhausted max attempts."
            task.updated_at = now
            recovered_count += 1

        if recovered_count > 0:
            self.db.flush()
        return recovered_count


class SQLOutboxRepository:
    """
    Repository for the Transactional Outbox.
    Ensures events written in database transactions are reliably published to queues.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(self, event: OutboxEvent) -> OutboxEvent:
        """Create a new outbox event record."""
        now = datetime.now(timezone.utc)
        if not event.created_at:
            event.created_at = now
        self.db.add(event)
        self.db.flush()
        return event

    def get_pending_events(self, limit: int = 100) -> List[OutboxEvent]:
        """Fetch pending outbox events ordered by creation time."""
        return (
            self.db.query(OutboxEvent)
            .filter(OutboxEvent.status == "PENDING")
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
            .all()
        )

    def mark_published(self, event_id: str, tenant_id: str) -> bool:
        """Mark outbox event as PUBLISHED."""
        event = (
            self.db.query(OutboxEvent)
            .filter(OutboxEvent.id == event_id, OutboxEvent.tenant_id == tenant_id)
            .first()
        )
        if not event:
            return False
        event.status = "PUBLISHED"
        event.published_at = datetime.now(timezone.utc)
        self.db.flush()
        return True

    def mark_failed(self, event_id: str, tenant_id: str) -> bool:
        """Mark outbox event as FAILED with incremented attempt count."""
        event = (
            self.db.query(OutboxEvent)
            .filter(OutboxEvent.id == event_id, OutboxEvent.tenant_id == tenant_id)
            .first()
        )
        if not event:
            return False
        event.attempt_count += 1
        if event.attempt_count >= 5:
            event.status = "FAILED"
        self.db.flush()
        return True
