"""
documents.py — Protected Document upload, ingestion, permission management, and deletion API endpoints.
Enforces multi-tenant isolation, deterministic authorization, content-hash idempotency, and lifecycle state persistence.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    Request,
    Depends,
    Form,
    Query,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config
from app.ingestion.loaders import SUPPORTED_EXTENSIONS
from app.ingestion.pipeline import ingest_document
from app.auth.models import User
from app.authorization.policy import require_permission, AuthorizationPolicy
from app.authorization.permissions import Permission
from app.security.audit import log_security_event
from app.storage.database import get_db, SessionLocal
from app.storage.blob import get_document_storage
from app.storage.models import (
    Tenant as DBTenant,
    Document as DBDocument,
    DocumentPermission as DBDocumentPermission,
    DocumentChunk as DBDocumentChunk,
    Task as DBTask,
    OutboxEvent as DBOutboxEvent,
)
from app.storage.repositories import (
    SQLTenantRepository,
    SQLDocumentRepository,
    SQLPermissionRepository,
    SQLChunkRepository,
    SQLTaskRepository,
    SQLOutboxRepository,
    DuplicateEntityException,
    ConcurrencyConflictException,
)
from app.tasks.models import TaskType
from app.tasks.queue import get_task_queue
from app.tasks.handlers.ingestion import DocumentIngestHandler
from app.tasks.handlers.deletion import DocumentDeleteHandler
from app.tasks.handlers.base import WorkerContext, NonRetryableTaskError, RetryableTaskError
from app.storage.search.factory import get_search_store
from app.connectors.secrets import get_secret_provider

router = APIRouter(prefix="/documents", tags=["documents"])

_MAX_FILE_SIZE_BYTES = config.MAX_FILE_SIZE_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Response & Request models
# ---------------------------------------------------------------------------

class IngestResponse(BaseModel):
    document_id: str
    filename: str
    file_type: str
    num_chunks: int
    status: str
    message: str = ""
    owner_id: Optional[str] = None
    tenant_id: Optional[str] = None
    access_level: Optional[str] = None
    task_id: Optional[str] = None


class DeleteResponse(BaseModel):
    document_id: str
    status: str
    message: str
    task_id: Optional[str] = None


class DocumentDetailResponse(BaseModel):
    document_id: str
    tenant_id: str
    owner_id: Optional[str]
    filename: str
    source: str
    source_document_id: Optional[str]
    size_bytes: int
    content_hash: str
    access_level: str
    permission_status: str
    status: str
    error_message: Optional[str]
    version: int
    num_chunks: int
    allowed_roles: List[str] = []
    allowed_user_ids: List[str] = []
    created_at: Optional[str]
    updated_at: Optional[str]
    indexed_at: Optional[str]


class PermissionUpdateRequest(BaseModel):
    access_level: Optional[str] = None  # PRIVATE, ROLE_BASED, PUBLIC
    allowed_roles: Optional[List[str]] = None
    allowed_user_ids: Optional[List[str]] = None
    expected_version: Optional[int] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/upload", response_model=IngestResponse, status_code=status.HTTP_200_OK)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    access_level: Optional[str] = Form("PRIVATE"),
    allowed_roles: Optional[str] = Form(None),
    allowed_user_ids: Optional[str] = Form(None),
    source: Optional[str] = Form("upload"),
    source_document_id: Optional[str] = Form(None),
    async_processing: Optional[bool] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.DOCUMENT_UPLOAD)),
):
    """
    Upload a document, persist metadata and binary content, execute chunking,
    and update vector/keyword indexes through authoritative lifecycle states:
    PENDING -> PROCESSING -> INDEXING -> INDEXED (or FAILED).
    Atomically writes Task and OutboxEvent records in PostgreSQL.
    """
    original_filename: str = file.filename or "unnamed"

    # ── 1. Validate file extension ──────────────────────────────────────────
    if "." not in original_filename:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file has no extension. Only named files are accepted.",
        )

    ext = original_filename.rsplit(".", 1)[-1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type: .{ext}. "
                f"Allowed formats: {sorted(SUPPORTED_EXTENSIONS)}"
            ),
        )

    # ── 2. Read and validate file content size ──────────────────────────────
    content: bytes = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    if len(content) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size ({len(content) / 1024 / 1024:.1f} MB) exceeds "
                f"the maximum allowed size of {config.MAX_FILE_SIZE_MB} MB."
            ),
        )

    content_hash = hashlib.sha256(content).hexdigest()
    doc_repo = SQLDocumentRepository(db)
    perm_repo = SQLPermissionRepository(db)
    chunk_repo = SQLChunkRepository(db)
    tenant_repo = SQLTenantRepository(db)
    task_repo = SQLTaskRepository(db)
    outbox_repo = SQLOutboxRepository(db)
    blob_storage = get_document_storage()

    # Ensure tenant exists in DB
    tenant_repo.get_or_create(current_user.tenant_id, name=current_user.tenant_id)

    # ── 3. Check Idempotency (duplicate source / content hash) ──────────────
    existing_doc: Optional[DBDocument] = None
    if source_document_id:
        existing_doc = doc_repo.get_by_source(
            tenant_id=current_user.tenant_id,
            source=source or "upload",
            source_document_id=source_document_id,
        )
    if not existing_doc:
        existing_doc = doc_repo.get_by_content_hash(
            tenant_id=current_user.tenant_id,
            content_hash=content_hash,
        )

    # If document already exists with identical content and is INDEXED, return idempotently
    if existing_doc and existing_doc.content_hash == content_hash and existing_doc.status == "INDEXED":
        chunks = chunk_repo.get_by_document(existing_doc.document_id, tenant_id=current_user.tenant_id)
        return IngestResponse(
            document_id=existing_doc.document_id,
            filename=existing_doc.filename,
            file_type=ext,
            num_chunks=len(chunks),
            status="success",
            message=f"Document already indexed (idempotent). {len(chunks)} chunk(s) available.",
            owner_id=existing_doc.owner_id,
            tenant_id=existing_doc.tenant_id,
            access_level=existing_doc.access_level,
        )

    # ── 4. Parse ACLs ────────────────────────────────────────────────────────
    roles_list = [r.strip().upper() for r in allowed_roles.split(",")] if allowed_roles else []
    users_list = [u.strip() for u in allowed_user_ids.split(",")] if allowed_user_ids else []

    # ── 5. Save binary to Blob Storage & Create/Update DB record (PENDING) ──
    storage_path = blob_storage.save(content, original_filename, current_user.tenant_id)

    if existing_doc:
        # Existing source document with updated content or retry
        document_id = existing_doc.document_id
        db_doc = existing_doc
        db_doc.filename = original_filename
        db_doc.content_hash = content_hash
        db_doc.size_bytes = len(content)
        db_doc.mime_type = f"application/{ext}"
        db_doc.storage_path = storage_path
        db_doc.access_level = access_level.upper() if access_level else "PRIVATE"
        db_doc.status = "PENDING"
        db_doc.error_message = None
        db_doc.version += 1
        db_doc.updated_at = datetime.now(timezone.utc)
        db.commit()
    else:
        document_id = content_hash
        db_doc = DBDocument(
            document_id=document_id,
            tenant_id=current_user.tenant_id,
            owner_id=current_user.user_id,
            filename=original_filename,
            source=source or "upload",
            source_document_id=source_document_id,
            mime_type=f"application/{ext}",
            size_bytes=len(content),
            content_hash=content_hash,
            access_level=access_level.upper() if access_level else "PRIVATE",
            permission_status="KNOWN",
            status="PENDING",
            storage_path=storage_path,
        )
        try:
            db_doc = doc_repo.create(db_doc)
            db.commit()
        except DuplicateEntityException:
            db.rollback()
            db_doc = doc_repo.get_by_id(document_id, tenant_id=current_user.tenant_id)
            if db_doc:
                db_doc.status = "PENDING"
                db_doc.error_message = None
                db_doc.storage_path = storage_path
                db.commit()

    # ── 6. Create Task & OutboxEvent Atomically in PostgreSQL ───────────────
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    task = DBTask(
        id=task_id,
        tenant_id=current_user.tenant_id,
        task_type=TaskType.DOCUMENT_INGEST.value,
        idempotency_key=f"ingest_{current_user.tenant_id}_{document_id}_{db_doc.version}",
        aggregate_id=document_id,
        status="PENDING",
        payload={
            "document_id": document_id,
            "tenant_id": current_user.tenant_id,
            "document_version": db_doc.version,
            "storage_path": storage_path,
            "owner_id": current_user.user_id,
            "access_level": db_doc.access_level,
            "allowed_roles": roles_list,
            "allowed_user_ids": users_list,
        },
    )
    task_repo.create(task)

    outbox_event = DBOutboxEvent(
        id=f"evt_{uuid.uuid4().hex[:16]}",
        tenant_id=current_user.tenant_id,
        event_type=TaskType.DOCUMENT_INGEST.value,
        aggregate_id=document_id,
        payload={"task_id": task_id, "document_id": document_id},
    )
    outbox_repo.create(outbox_event)
    db.commit()

    # ── 7. Async vs Synchronous Processing Dispatch ─────────────────────────
    if async_processing is True:
        task_queue = get_task_queue()
        return IngestResponse(
            document_id=document_id,
            filename=original_filename,
            file_type=ext,
            num_chunks=0,
            status="accepted",
            message=f"Document upload accepted for asynchronous processing (Task ID: {task_id}).",
            owner_id=current_user.user_id,
            tenant_id=current_user.tenant_id,
            access_level=db_doc.access_level,
            task_id=task_id,
        )

    # Synchronous processing using DocumentIngestHandler
    rag_service = getattr(request.app.state, "rag_service", None)
    search_store = getattr(request.app.state, "search_store", None) or get_search_store()
    worker_context = WorkerContext(
        db=db,
        search_store=search_store,
        blob_storage=blob_storage,
        secret_provider=get_secret_provider(),
        rag_service=rag_service,
        worker_id="api-handler",
    )
    handler = DocumentIngestHandler()
    try:
        result = handler.handle(task, worker_context)
        task_repo.mark_success(task_id, current_user.tenant_id, result)
        outbox_repo.mark_published(outbox_event.id, current_user.tenant_id)
        db.commit()
    except NonRetryableTaskError as exc:
        task_repo.mark_failure(task_id, current_user.tenant_id, str(exc), is_retryable=False)
        db.commit()
        if "Invalid JSON" in str(exc) or "JSONDecodeError" in str(exc) or "json" in ext.lower():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid JSON file format: {exc}",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        task_repo.mark_failure(task_id, current_user.tenant_id, str(exc), is_retryable=True)
        doc_repo.update_status(document_id, status="FAILED", error_message=str(exc))
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {exc}",
        )

    chunks = chunk_repo.get_by_document(document_id, tenant_id=current_user.tenant_id)

    return IngestResponse(
        document_id=document_id,
        filename=original_filename,
        file_type=ext,
        num_chunks=len(chunks),
        status="success",
        message=f"Document ingested successfully. {len(chunks)} chunk(s) produced.",
        owner_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
        access_level=db_doc.access_level,
        task_id=task_id,
    )


@router.patch("/{document_id}/permissions", response_model=DocumentDetailResponse, status_code=status.HTTP_200_OK)
async def update_document_permissions(
    document_id: str,
    payload: PermissionUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.DOCUMENT_UPLOAD)),
):
    """
    Permission-only update flow: update access level, allowed roles, and allowed user IDs
    in PostgreSQL metadata and chunk records without re-chunking or re-embedding.
    """
    doc_repo = SQLDocumentRepository(db)
    perm_repo = SQLPermissionRepository(db)
    chunk_repo = SQLChunkRepository(db)

    db_doc = doc_repo.get_by_id(document_id, tenant_id=current_user.tenant_id)
    if not db_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    # Authorization: Admins, managers, or document owners can update permissions
    is_admin_or_mgr = current_user.role.upper() in {"ADMIN", "MANAGER"}
    is_owner = db_doc.owner_id == current_user.user_id
    if not (is_admin_or_mgr or is_owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You are not authorized to update permissions for this document.",
        )

    # Optimistic concurrency check
    if payload.expected_version is not None and db_doc.version != payload.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Optimistic concurrency conflict on document '{document_id}'. "
                f"Expected version {payload.expected_version}, but found {db_doc.version}."
            ),
        )

    # Update document access level
    if payload.access_level:
        db_doc.access_level = payload.access_level.upper()

    # Update permissions table
    roles = payload.allowed_roles if payload.allowed_roles is not None else []
    users = payload.allowed_user_ids if payload.allowed_user_ids is not None else []
    perm_repo.set_permissions(
        document_id=document_id,
        tenant_id=current_user.tenant_id,
        roles=roles,
        user_ids=users,
    )

    # Update metadata in existing chunks without re-embedding
    chunks = chunk_repo.get_by_document(document_id, tenant_id=current_user.tenant_id)
    for c in chunks:
        meta = dict(c.metadata_json or {})
        if payload.access_level:
            meta["access_level"] = payload.access_level.upper()
        meta["allowed_roles"] = roles
        meta["allowed_user_ids"] = users
        c.metadata_json = meta

    db_doc.version += 1
    db_doc.updated_at = datetime.now(timezone.utc)
    db.commit()

    log_security_event(
        event_type="PERMISSION_UPDATE",
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action="update_permissions",
        result="success",
        document_id=document_id,
        details={
            "access_level": db_doc.access_level,
            "roles": roles,
            "user_ids": users,
            "version": db_doc.version,
        },
    )

    return DocumentDetailResponse(
        document_id=db_doc.document_id,
        tenant_id=db_doc.tenant_id,
        owner_id=db_doc.owner_id,
        filename=db_doc.filename,
        source=db_doc.source,
        source_document_id=db_doc.source_document_id,
        size_bytes=db_doc.size_bytes,
        content_hash=db_doc.content_hash,
        access_level=db_doc.access_level,
        permission_status=db_doc.permission_status,
        status=db_doc.status,
        error_message=db_doc.error_message,
        version=db_doc.version,
        num_chunks=len(chunks),
        allowed_roles=roles,
        allowed_user_ids=users,
        created_at=db_doc.created_at.isoformat() if db_doc.created_at else None,
        updated_at=db_doc.updated_at.isoformat() if db_doc.updated_at else None,
        indexed_at=db_doc.indexed_at.isoformat() if db_doc.indexed_at else None,
    )


@router.get("/{document_id}", response_model=DocumentDetailResponse, status_code=status.HTTP_200_OK)
async def get_document_details(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.DOCUMENT_READ)),
):
    """
    Get detailed document metadata, ingestion lifecycle status, version, and ACLs.
    """
    doc_repo = SQLDocumentRepository(db)
    perm_repo = SQLPermissionRepository(db)
    chunk_repo = SQLChunkRepository(db)

    db_doc = doc_repo.get_by_id(document_id, tenant_id=current_user.tenant_id)
    if not db_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    perms = perm_repo.get_for_document(document_id, tenant_id=current_user.tenant_id)
    allowed_roles = [p.role for p in perms if p.role]
    allowed_users = [p.user_id for p in perms if p.user_id]
    chunks = chunk_repo.get_by_document(document_id, tenant_id=current_user.tenant_id)

    return DocumentDetailResponse(
        document_id=db_doc.document_id,
        tenant_id=db_doc.tenant_id,
        owner_id=db_doc.owner_id,
        filename=db_doc.filename,
        source=db_doc.source,
        source_document_id=db_doc.source_document_id,
        size_bytes=db_doc.size_bytes,
        content_hash=db_doc.content_hash,
        access_level=db_doc.access_level,
        permission_status=db_doc.permission_status,
        status=db_doc.status,
        error_message=db_doc.error_message,
        version=db_doc.version,
        num_chunks=len(chunks),
        allowed_roles=allowed_roles,
        allowed_user_ids=allowed_users,
        created_at=db_doc.created_at.isoformat() if db_doc.created_at else None,
        updated_at=db_doc.updated_at.isoformat() if db_doc.updated_at else None,
        indexed_at=db_doc.indexed_at.isoformat() if db_doc.indexed_at else None,
    )


@router.get("", response_model=List[DocumentDetailResponse], status_code=status.HTTP_200_OK)
async def list_documents(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.DOCUMENT_READ)),
):
    """
    List documents within the caller's tenant with pagination and status filtering.
    """
    doc_repo = SQLDocumentRepository(db)
    perm_repo = SQLPermissionRepository(db)
    chunk_repo = SQLChunkRepository(db)

    docs = doc_repo.list_by_tenant(
        tenant_id=current_user.tenant_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    doc_ids = [d.document_id for d in docs]
    perms_map = perm_repo.batch_get_for_documents(doc_ids, tenant_id=current_user.tenant_id)

    results: List[DocumentDetailResponse] = []
    for d in docs:
        perms = perms_map.get(d.document_id, [])
        allowed_roles = [p.role for p in perms if p.role]
        allowed_users = [p.user_id for p in perms if p.user_id]
        chunks = chunk_repo.get_by_document(d.document_id, tenant_id=current_user.tenant_id)
        results.append(
            DocumentDetailResponse(
                document_id=d.document_id,
                tenant_id=d.tenant_id,
                owner_id=d.owner_id,
                filename=d.filename,
                source=d.source,
                source_document_id=d.source_document_id,
                size_bytes=d.size_bytes,
                content_hash=d.content_hash,
                access_level=d.access_level,
                permission_status=d.permission_status,
                status=d.status,
                error_message=d.error_message,
                version=d.version,
                num_chunks=len(chunks),
                allowed_roles=allowed_roles,
                allowed_user_ids=allowed_users,
                created_at=d.created_at.isoformat() if d.created_at else None,
                updated_at=d.updated_at.isoformat() if d.updated_at else None,
                indexed_at=d.indexed_at.isoformat() if d.indexed_at else None,
            )
        )
    return results


@router.delete("/{document_id}", response_model=DeleteResponse, status_code=status.HTTP_200_OK)
async def delete_document(
    document_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.DOCUMENT_DELETE)),
):
    """
    Clean cascading deletion:
    1. Verify tenant boundary and deletion authorization.
    2. Remove from vector index & BM25 index.
    3. Delete chunks, permissions, and document from DB.
    4. Delete stored binary file from DocumentStorage.
    5. Record security audit event.
    """
    doc_repo = SQLDocumentRepository(db)
    blob_storage = get_document_storage()

    # 1. Fetch document from DB
    db_doc = doc_repo.get_by_id(document_id, tenant_id=current_user.tenant_id)
    if not db_doc:
        # Fallback to vector DB metadata if document was ingested prior to persistent storage
        rag_service = getattr(request.app.state, "rag_service", None)
        found_in_vector = False
        doc_metadata: Dict[str, Any] = {}
        if rag_service and hasattr(rag_service.vector_db, "vectordb"):
            try:
                results = rag_service.vector_db.vectordb.get(where={"document_id": document_id})
                if results and results.get("ids"):
                    found_in_vector = True
                    metas = results.get("metadatas") or []
                    if metas:
                        doc_metadata = metas[0]
            except Exception:
                pass

        if not found_in_vector:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

        # Check tenant match on legacy vector doc
        doc_tenant = doc_metadata.get("tenant_id")
        if doc_tenant and doc_tenant != current_user.tenant_id:
            log_security_event(
                event_type="ACCESS_DENIED",
                tenant_id=current_user.tenant_id,
                user_id=current_user.user_id,
                action="delete",
                result="denied",
                document_id=document_id,
                details={"reason": "Cross-tenant deletion attempt", "target_tenant": doc_tenant},
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

        if not AuthorizationPolicy.can_delete_document(current_user, doc_metadata):
            log_security_event(
                event_type="ACCESS_DENIED",
                tenant_id=current_user.tenant_id,
                user_id=current_user.user_id,
                action="delete",
                result="denied",
                document_id=document_id,
                details={"reason": "User is not authorized to delete this document"},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You are not authorized to delete this document.",
            )

        # Delete from vector & BM25
        try:
            rag_service.vector_db.vectordb.delete(where={"document_id": document_id})
            rag_service.retrieval_pipeline.rebuild_bm25()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete document from index: {e}")

        log_security_event(
            event_type="DOCUMENT_DELETE",
            tenant_id=current_user.tenant_id,
            user_id=current_user.user_id,
            action="delete",
            result="success",
            document_id=document_id,
            details={"deleted_by": current_user.user_id},
        )
        return DeleteResponse(
            document_id=document_id,
            status="deleted",
            message="Document deleted successfully.",
        )

    # 2. Check deletion authorization policy
    doc_meta_for_policy = {
        "tenant_id": db_doc.tenant_id,
        "owner_id": db_doc.owner_id,
    }
    if not AuthorizationPolicy.can_delete_document(current_user, doc_meta_for_policy):
        log_security_event(
            event_type="ACCESS_DENIED",
            tenant_id=current_user.tenant_id,
            user_id=current_user.user_id,
            action="delete",
            result="denied",
            document_id=document_id,
            details={"reason": "User is not authorized to delete this document"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You are not authorized to delete this document.",
        )

    # 3. Create Task & OutboxEvent Atomically
    task_repo = SQLTaskRepository(db)
    outbox_repo = SQLOutboxRepository(db)
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    task = DBTask(
        id=task_id,
        tenant_id=current_user.tenant_id,
        task_type=TaskType.DOCUMENT_DELETE.value,
        idempotency_key=f"delete_{current_user.tenant_id}_{document_id}_{db_doc.version}",
        aggregate_id=document_id,
        status="PENDING",
        payload={
            "document_id": document_id,
            "tenant_id": current_user.tenant_id,
            "user_id": current_user.user_id,
            "storage_path": db_doc.storage_path,
        },
    )
    task_repo.create(task)
    outbox_event = DBOutboxEvent(
        id=f"evt_{uuid.uuid4().hex[:16]}",
        tenant_id=current_user.tenant_id,
        event_type=TaskType.DOCUMENT_DELETE.value,
        aggregate_id=document_id,
        payload={"task_id": task_id, "document_id": document_id},
    )
    outbox_repo.create(outbox_event)
    doc_repo.update_status(document_id, status="DELETING")
    db.commit()

    # 4. Execute cascading deletion via DocumentDeleteHandler
    rag_service = getattr(request.app.state, "rag_service", None)
    search_store = getattr(request.app.state, "search_store", None) or get_search_store()
    worker_context = WorkerContext(
        db=db,
        search_store=search_store,
        blob_storage=blob_storage,
        secret_provider=get_secret_provider(),
        rag_service=rag_service,
        worker_id="api-handler",
    )
    handler = DocumentDeleteHandler()
    result = handler.handle(task, worker_context)
    task_repo.mark_success(task_id, current_user.tenant_id, result)
    outbox_repo.mark_published(outbox_event.id, current_user.tenant_id)
    db.commit()

    return DeleteResponse(
        document_id=document_id,
        status="deleted",
        message="Document deleted successfully.",
        task_id=task_id,
    )
