"""
health.py — Health and readiness probe endpoints for orchestration and monitoring.
"""

from fastapi import APIRouter, Request, status, Response
from sqlalchemy import text
from app.storage.database import SessionLocal
from app.observability.errors import sanitize_error_detail

router = APIRouter(tags=["health"])


@router.get("/health")
@router.get("/api/health")
async def health_check():
    """General liveness heartbeat endpoint (zero dependency calls)."""
    return {"status": "healthy"}


@router.get("/health/live")
async def liveness_probe():
    """Liveness probe indicating the process is running (zero external calls)."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness_probe(request: Request, response: Response):
    """
    Lightweight readiness probe verifying critical dependencies for ALB health checking.
    Performs fast connectivity checks only; NEVER executes expensive RAG queries,
    KMS encryption, external LLM calls, or connector network calls.
    """
    checks = {
        "database": "unknown",
        "search_store": "unknown",
        "task_subsystem": "unknown",
        "document_storage": "unknown",
        "secret_provider": "unknown",
    }
    is_ready = True

    # 1. Database lightweight ping (SELECT 1)
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["database"] = "healthy"
        finally:
            db.close()
    except Exception as e:
        err_info = sanitize_error_detail(e)
        checks["database"] = f"unhealthy: {err_info.get('error_class', 'DatabaseError')}"
        is_ready = False

    # 2. Search Store lightweight check
    search_store = getattr(request.app.state, "search_store", None)
    rag_service = getattr(request.app.state, "rag_service", None)
    if not search_store and rag_service:
        search_store = getattr(rag_service, "search_store", None)

    if search_store and hasattr(search_store, "health_check"):
        try:
            h = search_store.health_check()
            checks["search_store"] = h.status
            if h.status not in ("HEALTHY", "DEGRADED"):
                is_ready = False
        except Exception as e:
            err_info = sanitize_error_detail(e)
            checks["search_store"] = f"unhealthy: {err_info.get('error_class', 'SearchStoreError')}"
            is_ready = False
    else:
        checks["search_store"] = "healthy"

    # 3. Task Subsystem lightweight check
    try:
        from app.tasks.queue import get_task_queue
        t_queue = get_task_queue()
        checks["task_subsystem"] = "healthy" if t_queue else "not_configured"
    except Exception as e:
        err_info = sanitize_error_detail(e)
        checks["task_subsystem"] = f"unhealthy: {err_info.get('error_class', 'TaskQueueError')}"
        is_ready = False

    # 4. Document Storage config check
    try:
        from app.storage.blob import get_document_storage
        storage = get_document_storage()
        if hasattr(storage, "health_check"):
            storage_health = storage.health_check()
            checks["document_storage"] = storage_health.get("status", "unknown")
            if storage_health.get("status") not in ("HEALTHY", "DEGRADED"):
                is_ready = False
        else:
            checks["document_storage"] = "healthy"
    except Exception as e:
        err_info = sanitize_error_detail(e)
        checks["document_storage"] = f"unhealthy: {err_info.get('error_class', 'StorageError')}"
        is_ready = False

    # 5. Secret Provider config check
    try:
        from app.connectors.secrets import get_secret_provider
        secrets_prov = get_secret_provider()
        if hasattr(secrets_prov, "health_check"):
            secrets_health = secrets_prov.health_check()
            checks["secret_provider"] = secrets_health.get("status", "unknown")
            if secrets_health.get("status") not in ("HEALTHY", "DEGRADED"):
                is_ready = False
        else:
            checks["secret_provider"] = "healthy"
    except Exception as e:
        err_info = sanitize_error_detail(e)
        checks["secret_provider"] = f"unhealthy: {err_info.get('error_class', 'SecretProviderError')}"
        is_ready = False

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "checks": checks}

    return {"status": "ready", "checks": checks}


@router.get("/health/dependencies")
async def detailed_dependencies_check(request: Request, response: Response):
    """
    Detailed internal dependency health endpoint for administrative observability.
    Not polled by ALB at high frequency.
    """
    res = await readiness_probe(request, response)
    return {"status": res["status"], "details": res.get("checks", {})}

