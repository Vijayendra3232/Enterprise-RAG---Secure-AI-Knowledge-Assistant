"""
tasks.py — Protected Task Status and Management API endpoints.
Enforces multi-tenant isolation, IDOR prevention, and sanitized response payloads (zero secrets).
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.storage.database import get_db
from app.storage.repositories.task_repository import SQLTaskRepository
from app.auth.models import User
from app.auth.dependencies import get_current_user
from app.tasks.models import TaskResponse, TaskListResponse, TaskCancelResponse

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskResponse, status_code=status.HTTP_200_OK)
async def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get task status and metadata.
    Strictly tenant-scoped: cannot inspect tasks belonging to other tenants (returns 404 on IDOR).
    Sanitized: returns zero plaintext credentials, passwords, or decrypted secrets.
    """
    repo = SQLTaskRepository(db)
    task = repo.get_by_id_and_tenant(task_id, tenant_id=current_user.tenant_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    return TaskResponse(
        id=task.id,
        tenant_id=task.tenant_id,
        task_type=task.task_type,
        status=task.status,
        idempotency_key=task.idempotency_key,
        aggregate_id=task.aggregate_id,
        attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        created_at=task.created_at.isoformat() if task.created_at else "",
        available_at=task.available_at.isoformat() if task.available_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        failed_at=task.failed_at.isoformat() if task.failed_at else None,
        last_error=task.last_error,
        result=task.result,
    )


@router.get("", response_model=TaskListResponse, status_code=status.HTTP_200_OK)
async def list_tasks(
    status_filter: Optional[str] = Query(None, alias="status"),
    task_type: Optional[str] = Query(None, alias="type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List tasks within the caller's tenant with pagination and filters.
    """
    repo = SQLTaskRepository(db)
    tasks = repo.list_by_tenant(
        tenant_id=current_user.tenant_id,
        status=status_filter,
        task_type=task_type,
        limit=limit,
        offset=offset,
    )
    total = repo.count_by_tenant(
        tenant_id=current_user.tenant_id,
        status=status_filter,
        task_type=task_type,
    )

    items = [
        TaskResponse(
            id=t.id,
            tenant_id=t.tenant_id,
            task_type=t.task_type,
            status=t.status,
            idempotency_key=t.idempotency_key,
            aggregate_id=t.aggregate_id,
            attempt_count=t.attempt_count,
            max_attempts=t.max_attempts,
            created_at=t.created_at.isoformat() if t.created_at else "",
            available_at=t.available_at.isoformat() if t.available_at else None,
            started_at=t.started_at.isoformat() if t.started_at else None,
            completed_at=t.completed_at.isoformat() if t.completed_at else None,
            failed_at=t.failed_at.isoformat() if t.failed_at else None,
            last_error=t.last_error,
            result=t.result,
        )
        for t in tasks
    ]

    return TaskListResponse(
        tasks=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse, status_code=status.HTTP_200_OK)
async def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Cancel a pending or retrying task.
    """
    repo = SQLTaskRepository(db)
    task = repo.get_by_id_and_tenant(task_id, tenant_id=current_user.tenant_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    success = repo.cancel_task(task_id, tenant_id=current_user.tenant_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task '{task_id}' cannot be cancelled in state '{task.status}'.",
        )
    db.commit()

    return TaskCancelResponse(
        task_id=task_id,
        status="CANCELLED",
        message="Task successfully cancelled.",
    )
