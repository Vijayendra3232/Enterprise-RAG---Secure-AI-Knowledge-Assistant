"""
models.py — Task domain models, enums, and API request/response schemas.
"""

from enum import Enum
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class TaskType(str, Enum):
    DOCUMENT_INGEST = "DOCUMENT_INGEST"
    DOCUMENT_DELETE = "DOCUMENT_DELETE"
    DOCUMENT_REINDEX = "DOCUMENT_REINDEX"
    CONNECTOR_SYNC = "CONNECTOR_SYNC"
    PERMISSION_SYNC = "PERMISSION_SYNC"
    INDEX_REBUILD = "INDEX_REBUILD"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskCreate(BaseModel):
    task_type: TaskType
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    idempotency_key: Optional[str] = None
    aggregate_id: Optional[str] = None
    max_attempts: int = 3
    delay_seconds: int = 0


class TaskResponse(BaseModel):
    id: str
    tenant_id: str
    task_type: str
    status: str
    idempotency_key: Optional[str] = None
    aggregate_id: Optional[str] = None
    attempt_count: int
    max_attempts: int
    created_at: str
    available_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_at: Optional[str] = None
    last_error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None

    model_config = {"from_attributes": True}


class TaskListResponse(BaseModel):
    tasks: List[TaskResponse]
    total: int
    limit: int
    offset: int


class TaskCancelResponse(BaseModel):
    task_id: str
    status: str
    message: str


class OutboxEventCreate(BaseModel):
    tenant_id: str
    event_type: str
    aggregate_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
