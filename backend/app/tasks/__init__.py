"""
tasks package — Distributed Asynchronous Task Execution and Worker Architecture.
"""

from app.tasks.models import (
    TaskType,
    TaskStatus,
    TaskCreate,
    TaskResponse,
    TaskListResponse,
    TaskCancelResponse,
    OutboxEventCreate,
)
from app.tasks.queue import (
    TaskQueueInterface,
    DatabaseTaskQueue,
    InMemoryTaskQueue,
    get_task_queue,
    set_task_queue,
    reset_task_queue,
)
from app.tasks.dispatcher import OutboxDispatcher
from app.tasks.worker import WorkerRunner, WorkerPool
from app.tasks.handlers.base import (
    TaskHandler,
    WorkerContext,
    RetryableTaskError,
    NonRetryableTaskError,
)

__all__ = [
    "TaskType",
    "TaskStatus",
    "TaskCreate",
    "TaskResponse",
    "TaskListResponse",
    "TaskCancelResponse",
    "OutboxEventCreate",
    "TaskQueueInterface",
    "DatabaseTaskQueue",
    "InMemoryTaskQueue",
    "get_task_queue",
    "set_task_queue",
    "reset_task_queue",
    "OutboxDispatcher",
    "WorkerRunner",
    "WorkerPool",
    "TaskHandler",
    "WorkerContext",
    "RetryableTaskError",
    "NonRetryableTaskError",
]
