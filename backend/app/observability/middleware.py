"""
middleware.py — FastAPI Observability Middleware and Protected /metrics Endpoint Handler.
Enforces sensitive-header exclusion at the request boundary, manages correlation context,
records API metrics, and protects Prometheus/OpenMetrics exposition.
"""

import time
from typing import Optional, Set
from fastapi import Request, Response, APIRouter, status, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app import config
from app.observability.context import (
    set_request_context,
    clear_request_context,
    validate_or_generate_request_id,
    generate_trace_id,
    get_request_id,
    get_trace_id,
    get_safe_tenant_id,
    get_safe_user_id,
)
from app.observability.tracing import tracer
from app.observability.metrics import (
    metric_registry,
    http_requests_total,
    http_request_duration_seconds,
    http_exceptions_total,
)
from app.observability.schemas import APITelemetry
from app.observability.logging import structured_logger
from app.observability.errors import sanitize_error_detail, classify_error

# Explicit list of endpoints mapped to coarse groups to prevent metric label cardinality explosion
def get_endpoint_group(path: str) -> str:
    if path.startswith("/auth"):
        return "auth"
    elif path.startswith("/Chat-Assistant") or path.startswith("/chat"):
        return "chat"
    elif path.startswith("/documents"):
        return "documents"
    elif path.startswith("/connectors"):
        return "connectors"
    elif path.startswith("/tasks"):
        return "tasks"
    elif path.startswith("/health"):
        return "health"
    elif path.startswith("/metrics"):
        return "metrics"
    return "other"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Core HTTP middleware providing:
    1. Immediate sensitive header exclusion (Authorization, X-Monitoring-Token, Cookie).
    2. Request & trace context initialization.
    3. Monotonic latency measurement & API metrics.
    4. Structured access logging.
    5. Sanitized uncaught exception handling.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        t_start = time.perf_counter()

        # 1. Extract and validate X-Request-ID (or generate secure UUID)
        client_req_id = request.headers.get("X-Request-ID") or request.headers.get("x-request-id")
        req_id = validate_or_generate_request_id(client_req_id)
        trace_id = generate_trace_id()

        # Set execution context
        set_request_context(request_id=req_id, trace_id=trace_id)

        endpoint_group = get_endpoint_group(request.url.path)
        method = request.method.upper()

        # Root trace span for HTTP request
        span_attributes = {
            "http.method": method,
            "http.endpoint_group": endpoint_group,
            "http.url_path": request.url.path[:64],
        }

        with tracer.start_span("http_request", attributes=span_attributes) as span:
            try:
                response = await call_next(request)
                duration_ms = round((time.perf_counter() - t_start) * 1000, 3)
                status_code = response.status_code
                status_class = f"{status_code // 100}xx"

                # Attach correlation headers to response
                response.headers["X-Request-ID"] = req_id
                response.headers["X-Trace-ID"] = trace_id

                # Update API metrics
                http_requests_total.inc(
                    amount=1.0,
                    labels={"method": method, "status_class": status_class, "endpoint_group": endpoint_group},
                )
                http_request_duration_seconds.observe(
                    value=duration_ms / 1000.0,
                    labels={"method": method, "endpoint_group": endpoint_group},
                )

                if span:
                    span.set_attribute("http.status_code", status_code)
                    if status_code >= 400:
                        span.set_status("ERROR")

                # Structured access log
                telemetry = APITelemetry(
                    endpoint_group=endpoint_group,
                    method=method,
                    status_code=status_code,
                    status_class=status_class,
                    duration_ms=duration_ms,
                    request_id=req_id,
                    trace_id=trace_id,
                    safe_tenant_id=get_safe_tenant_id(),
                    safe_user_id=get_safe_user_id(),
                )

                log_level = "WARNING" if status_code >= 400 else "INFO"
                structured_logger.log_event(
                    event="request.completed",
                    level=log_level,
                    duration_ms=duration_ms,
                    status=str(status_code),
                    telemetry_model=telemetry,
                )

                return response

            except Exception as exc:
                duration_ms = round((time.perf_counter() - t_start) * 1000, 3)
                err_taxonomy = classify_error(exc)

                http_exceptions_total.inc(
                    amount=1.0,
                    labels={"error_class": err_taxonomy.value, "endpoint_group": endpoint_group},
                )

                if span:
                    span.set_status("ERROR")
                    span.set_attribute("error.class", err_taxonomy.value)

                structured_logger.log_event(
                    event="request.failed",
                    level="ERROR",
                    duration_ms=duration_ms,
                    status="500",
                    error_class=err_taxonomy.value,
                    error_code="INTERNAL_SERVER_ERROR",
                )

                # Return sanitized error response
                error_body = sanitize_error_detail(exc, request_id=req_id)
                res = JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content=error_body,
                )
                res.headers["X-Request-ID"] = req_id
                res.headers["X-Trace-ID"] = trace_id
                return res

            finally:
                clear_request_context()


# ─── Protected /metrics Router ───────────────────────────────────────────────

metrics_router = APIRouter(tags=["monitoring"])


@metrics_router.get("/metrics")
async def get_metrics(request: Request):
    """
    Exposes Prometheus/OpenMetrics formatted telemetry.
    Protected by dedicated monitoring authentication; never echoes monitoring credentials.
    """
    configured_key = getattr(config, "MONITORING_API_KEY", "")
    env = getattr(config, "ENVIRONMENT", "development").lower()

    # Extract monitoring token from X-Monitoring-Token, X-Monitoring-Key, or Bearer Authorization
    token = ""
    mon_header = (
        request.headers.get("X-Monitoring-Token")
        or request.headers.get("x-monitoring-token")
        or request.headers.get("X-Monitoring-Key")
        or request.headers.get("x-monitoring-key")
    )
    if mon_header:
        token = mon_header.strip()
    else:
        auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    is_authorized = False

    if configured_key:
        if token and token == configured_key:
            is_authorized = True
    else:
        # If key is not configured, allow ONLY in development environment
        if env == "development":
            is_authorized = True
        else:
            is_authorized = False

    if not is_authorized:
        # Fail closed with generic 401 response without leaking token details
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized monitoring request.",
        )

    content = metric_registry.to_openmetrics()
    return PlainTextResponse(content=content, media_type="text/plain; version=0.0.4")
