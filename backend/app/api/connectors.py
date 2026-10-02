"""
connectors.py — Protected Connector Administration API endpoints.
Provides CRUD for connector definitions, manual and incremental sync triggers,
and execution status inspection with strict tenant isolation and RBAC.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.models import User
from app.authorization.policy import require_permission
from app.authorization.permissions import Permission
from app.storage.database import get_db
from app.storage.models import (
    ConnectorConfig as DBConnectorConfig,
    SyncRun as DBSyncRun,
    Task as DBTask,
    OutboxEvent as DBOutboxEvent,
)
from app.storage.repositories import (
    SQLConnectorRepository,
    SQLSyncRunRepository,
    SQLTaskRepository,
    SQLOutboxRepository,
    DuplicateEntityException,
    EntityNotFoundException,
)
from app.tasks.models import TaskType
from app.connectors.secrets import get_secret_provider
from app.connectors.sync import SyncService, SyncMode, SyncResult
from app.connectors.errors import (
    ConnectorNotFoundError,
    ConnectorError,
)
from app.security.audit import log_security_event

router = APIRouter(prefix="/connectors", tags=["connectors"])


# ---------------------------------------------------------------------------
# Request & Response Models
# ---------------------------------------------------------------------------

class ConnectorCreateRequest(BaseModel):
    name: str
    connector_type: str = "local"
    config: Dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdateRequest(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None  # ACTIVE, DISABLED
    config: Optional[Dict[str, Any]] = None


class ConnectorResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    connector_type: str
    status: str
    last_sync_at: Optional[str]
    created_at: str
    updated_at: str


class SyncTriggerRequest(BaseModel):
    mode: str = "FULL"  # "FULL" or "INCREMENTAL"


class SyncRunResponse(BaseModel):
    id: str
    connector_id: str
    tenant_id: str
    sync_mode: str
    status: str
    started_at: str
    completed_at: Optional[str]
    documents_seen: int
    documents_added: int
    documents_updated: int
    documents_deleted: int
    permissions_updated: int
    documents_skipped: int
    errors: List[Dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=ConnectorResponse, status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: ConnectorCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Register a new data source connector for the caller's tenant.
    Encrypts sensitive credentials before persistence.
    """
    secret_provider = get_secret_provider()
    repo = SQLConnectorRepository(db)

    connector_id = f"conn_{uuid.uuid4().hex[:12]}"
    encrypted_config = secret_provider.encrypt_json(payload.config)

    db_conn = DBConnectorConfig(
        id=connector_id,
        tenant_id=current_user.tenant_id,
        name=payload.name,
        connector_type=payload.connector_type.lower().strip(),
        status="ACTIVE",
        encrypted_config=encrypted_config,
    )

    try:
        db_conn = repo.create(db_conn)
        db.commit()
    except DuplicateEntityException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    log_security_event(
        event_type="CONNECTOR_CREATED",
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action="create_connector",
        result="success",
        details={"connector_id": connector_id, "name": payload.name, "type": payload.connector_type},
    )

    return ConnectorResponse(
        id=db_conn.id,
        tenant_id=db_conn.tenant_id,
        name=db_conn.name,
        connector_type=db_conn.connector_type,
        status=db_conn.status,
        last_sync_at=db_conn.last_sync_at.isoformat() if db_conn.last_sync_at else None,
        created_at=db_conn.created_at.isoformat(),
        updated_at=db_conn.updated_at.isoformat(),
    )


@router.get("", response_model=List[ConnectorResponse], status_code=status.HTTP_200_OK)
async def list_connectors(
    connector_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    List all configured connectors within the caller's tenant.
    """
    repo = SQLConnectorRepository(db)
    conns = repo.list_by_tenant(
        tenant_id=current_user.tenant_id,
        connector_type=connector_type,
        limit=limit,
        offset=offset,
    )

    return [
        ConnectorResponse(
            id=c.id,
            tenant_id=c.tenant_id,
            name=c.name,
            connector_type=c.connector_type,
            status=c.status,
            last_sync_at=c.last_sync_at.isoformat() if c.last_sync_at else None,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
        )
        for c in conns
    ]


@router.get("/{connector_id}", response_model=ConnectorResponse, status_code=status.HTTP_200_OK)
async def get_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Retrieve details of a specific connector configuration.
    """
    repo = SQLConnectorRepository(db)
    db_conn = repo.get_by_id(connector_id, tenant_id=current_user.tenant_id)
    if not db_conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    return ConnectorResponse(
        id=db_conn.id,
        tenant_id=db_conn.tenant_id,
        name=db_conn.name,
        connector_type=db_conn.connector_type,
        status=db_conn.status,
        last_sync_at=db_conn.last_sync_at.isoformat() if db_conn.last_sync_at else None,
        created_at=db_conn.created_at.isoformat(),
        updated_at=db_conn.updated_at.isoformat(),
    )


@router.patch("/{connector_id}", response_model=ConnectorResponse, status_code=status.HTTP_200_OK)
async def update_connector(
    connector_id: str,
    payload: ConnectorUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Update connector metadata, credentials, or state.
    """
    repo = SQLConnectorRepository(db)
    db_conn = repo.get_by_id(connector_id, tenant_id=current_user.tenant_id)
    if not db_conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    if payload.name is not None:
        db_conn.name = payload.name
    if payload.status is not None:
        db_conn.status = payload.status.upper()
    if payload.config is not None:
        secret_provider = get_secret_provider()
        db_conn.encrypted_config = secret_provider.encrypt_json(payload.config)

    db_conn.updated_at = datetime.now(timezone.utc)
    db.commit()

    log_security_event(
        event_type="CONNECTOR_UPDATED",
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action="update_connector",
        result="success",
        details={"connector_id": connector_id, "status": db_conn.status},
    )

    return ConnectorResponse(
        id=db_conn.id,
        tenant_id=db_conn.tenant_id,
        name=db_conn.name,
        connector_type=db_conn.connector_type,
        status=db_conn.status,
        last_sync_at=db_conn.last_sync_at.isoformat() if db_conn.last_sync_at else None,
        created_at=db_conn.created_at.isoformat(),
        updated_at=db_conn.updated_at.isoformat(),
    )


@router.get("/{connector_id}/health", status_code=status.HTTP_200_OK)
async def check_connector_health(
    connector_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Verify connector provider credentials and connectivity without exposing secrets.
    """
    repo = SQLConnectorRepository(db)
    db_conn = repo.get_by_id(connector_id, tenant_id=current_user.tenant_id)
    if not db_conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    secret_provider = get_secret_provider()
    try:
        config_dict = secret_provider.decrypt_json(db_conn.encrypted_config)
        from app.connectors.registry import connector_registry
        connector = connector_registry.create(
            connector_type=db_conn.connector_type,
            tenant_id=current_user.tenant_id,
            connector_id=connector_id,
            config=config_dict,
        )
        health = connector.health_check()
        return health
    except Exception as exc:
        return {
            "status": "UNHEALTHY",
            "connector_id": connector_id,
            "tenant_id": current_user.tenant_id,
            "error": str(exc),
        }


@router.delete("/{connector_id}", status_code=status.HTTP_200_OK)
async def delete_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Delete connector configuration and its historical sync logs.
    """
    repo = SQLConnectorRepository(db)
    deleted = repo.delete(connector_id, tenant_id=current_user.tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")
    db.commit()

    log_security_event(
        event_type="CONNECTOR_DELETED",
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action="delete_connector",
        result="success",
        details={"connector_id": connector_id},
    )
    return {"status": "deleted", "connector_id": connector_id}


@router.post("/{connector_id}/sync", response_model=SyncRunResponse, status_code=status.HTTP_200_OK)
async def trigger_sync(
    connector_id: str,
    payload: SyncTriggerRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Trigger manual full or incremental synchronization for a connector.
    Creates Task and OutboxEvent records in PostgreSQL.
    """
    conn_repo = SQLConnectorRepository(db)
    db_conn = conn_repo.get_by_id(connector_id, tenant_id=current_user.tenant_id)
    if not db_conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    task_repo = SQLTaskRepository(db)
    outbox_repo = SQLOutboxRepository(db)
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    mode_str = payload.mode.upper() if payload.mode else "FULL"
    mode = SyncMode.INCREMENTAL if mode_str == "INCREMENTAL" else SyncMode.FULL

    # 1. Create Task & OutboxEvent records in PostgreSQL
    task = DBTask(
        id=task_id,
        tenant_id=current_user.tenant_id,
        task_type=TaskType.CONNECTOR_SYNC.value,
        idempotency_key=f"sync_{current_user.tenant_id}_{connector_id}_{int(datetime.now(timezone.utc).timestamp())}",
        aggregate_id=connector_id,
        status="PENDING",
        payload={
            "connector_id": connector_id,
            "tenant_id": current_user.tenant_id,
            "mode": mode.value,
            "user_id": current_user.user_id,
        },
    )
    task_repo.create(task)

    outbox_event = DBOutboxEvent(
        id=f"evt_{uuid.uuid4().hex[:16]}",
        tenant_id=current_user.tenant_id,
        event_type=TaskType.CONNECTOR_SYNC.value,
        aggregate_id=connector_id,
        payload={"task_id": task_id, "connector_id": connector_id},
    )
    outbox_repo.create(outbox_event)
    db.commit()

    sync_service = SyncService(db)
    rag_service = getattr(request.app.state, "rag_service", None)

    try:
        result: SyncResult = sync_service.sync(
            connector_id=connector_id,
            tenant_id=current_user.tenant_id,
            mode=mode,
            user_id=current_user.user_id,
            rag_service=rag_service,
        )
        task_repo.mark_success(task_id, current_user.tenant_id, {
            "status": result.status,
            "documents_seen": result.documents_seen,
            "documents_added": result.documents_added,
        })
        outbox_repo.mark_published(outbox_event.id, current_user.tenant_id)
        db.commit()
    except ConnectorNotFoundError as exc:
        task_repo.mark_failure(task_id, current_user.tenant_id, str(exc), is_retryable=False)
        db.commit()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ConcurrentSyncError as exc:
        task_repo.mark_failure(task_id, current_user.tenant_id, str(exc), is_retryable=True)
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConnectorError as exc:
        task_repo.mark_failure(task_id, current_user.tenant_id, str(exc), is_retryable=True)
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return SyncRunResponse(
        id=result.run_id,
        connector_id=result.connector_id,
        tenant_id=result.tenant_id,
        sync_mode=result.sync_mode.value,
        status=result.status,
        started_at=result.started_at.isoformat(),
        completed_at=result.completed_at.isoformat() if result.completed_at else None,
        documents_seen=result.documents_seen,
        documents_added=result.documents_added,
        documents_updated=result.documents_updated,
        documents_deleted=result.documents_deleted,
        permissions_updated=result.permissions_updated,
        documents_skipped=result.documents_skipped,
        errors=result.errors,
    )


@router.get("/{connector_id}/sync-status", response_model=Optional[SyncRunResponse], status_code=status.HTTP_200_OK)
async def get_sync_status(
    connector_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    Get latest sync execution status and counts for a connector.
    """
    repo = SQLConnectorRepository(db)
    db_conn = repo.get_by_id(connector_id, tenant_id=current_user.tenant_id)
    if not db_conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    sync_repo = SQLSyncRunRepository(db)
    latest_run = sync_repo.get_latest_by_connector(connector_id, tenant_id=current_user.tenant_id)
    if not latest_run:
        return None

    return SyncRunResponse(
        id=latest_run.id,
        connector_id=latest_run.connector_id,
        tenant_id=latest_run.tenant_id,
        sync_mode=latest_run.sync_mode,
        status=latest_run.status,
        started_at=latest_run.started_at.isoformat(),
        completed_at=latest_run.completed_at.isoformat() if latest_run.completed_at else None,
        documents_seen=latest_run.documents_seen,
        documents_added=latest_run.documents_added,
        documents_updated=latest_run.documents_updated,
        documents_deleted=latest_run.documents_deleted,
        permissions_updated=latest_run.permissions_updated,
        documents_skipped=latest_run.documents_skipped,
        errors=latest_run.errors_json or [],
    )


@router.get("/{connector_id}/runs", response_model=List[SyncRunResponse], status_code=status.HTTP_200_OK)
async def list_sync_runs(
    connector_id: str,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.SETTINGS_MANAGE)),
):
    """
    List historical sync runs for a connector.
    """
    repo = SQLConnectorRepository(db)
    db_conn = repo.get_by_id(connector_id, tenant_id=current_user.tenant_id)
    if not db_conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    sync_repo = SQLSyncRunRepository(db)
    runs = sync_repo.list_by_connector(connector_id, tenant_id=current_user.tenant_id, limit=limit, offset=offset)

    return [
        SyncRunResponse(
            id=r.id,
            connector_id=r.connector_id,
            tenant_id=r.tenant_id,
            sync_mode=r.sync_mode,
            status=r.status,
            started_at=r.started_at.isoformat(),
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            documents_seen=r.documents_seen,
            documents_added=r.documents_added,
            documents_updated=r.documents_updated,
            documents_deleted=r.documents_deleted,
            permissions_updated=r.permissions_updated,
            documents_skipped=r.documents_skipped,
            errors=r.errors_json or [],
        )
        for r in runs
    ]
