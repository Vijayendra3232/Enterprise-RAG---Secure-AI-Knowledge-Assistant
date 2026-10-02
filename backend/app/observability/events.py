"""
events.py — Security and operational event telemetry.
Provides structured event emission for authentication anomalies, authorization violations,
SSRF blocks, and operational milestones, integrating directly with security audit logging
and metrics collection.
"""

from typing import Dict, Any, Optional, Set
from app.observability.schemas import SecurityTelemetry, SafeTelemetryBase
from app.observability.redaction import SafeIdentityHasher, redact_data
from app.observability.logging import structured_logger
from app.observability.context import (
    get_request_id,
    get_trace_id,
    get_safe_tenant_id,
    get_safe_user_id,
)
from app.observability.metrics import security_events_total

ALLOWED_SECURITY_EVENT_TYPES: Set[str] = {
    "AUTH_FAILURE",
    "INVALID_JWT",
    "INVALID_JWT_ALGORITHM",
    "JWT_SIGNATURE_FAILURE",
    "JWT_ISSUER_FAILURE",
    "JWT_AUDIENCE_FAILURE",
    "JWT_EXPIRATION_FAILURE",
    "TENANT_MISMATCH",
    "AUTHZ_DENIED",
    "CROSS_TENANT_ATTEMPT",
    "UNKNOWN_PERMISSION_FAIL_CLOSED",
    "SSRF_BLOCKED",
    "CONNECTOR_AUTH_FAILURE",
    "PERMISSION_REVOCATION",
    "CREDENTIAL_DECRYPTION_FAILURE",
}


def emit_security_event(
    event_type: str,
    action: str,
    result: str,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    document_id: Optional[str] = None,
    severity: str = "WARNING",
) -> Dict[str, Any]:
    """
    Emits a structured security event with safe, non-reversible identity hashes.
    Increments metrics and records audit trails without exposing credentials or document content.
    """
    clean_event_type = event_type if event_type in ALLOWED_SECURITY_EVENT_TYPES else "SECURITY_EVENT"
    safe_tenant = SafeIdentityHasher.hash_tenant_id(tenant_id) if tenant_id else get_safe_tenant_id()
    safe_user = SafeIdentityHasher.hash_user_id(user_id) if user_id else get_safe_user_id()

    sanitized_details = redact_data(details or {})
    if document_id:
        # Include safe document hash/identifier
        sanitized_details["document_id"] = document_id

    sec_telemetry = SecurityTelemetry(
        event_type=clean_event_type,
        severity=severity.upper(),
        action=action,
        result=result,
        details=sanitized_details if sanitized_details else None,
    )

    # Increment metric with bounded labels
    security_events_total.inc(
        amount=1.0,
        labels={"event_type": clean_event_type, "result": result.lower()},
    )

    # Structured log line
    log_level = "ERROR" if severity.upper() in {"ERROR", "CRITICAL"} else "WARNING"
    log_record = structured_logger.log_event(
        event=f"security.{clean_event_type.lower()}",
        level=log_level,
        status=result.lower(),
        telemetry_model=sec_telemetry,
    )

    # Forward to existing audit logger for persistent audit records if available
    try:
        from app.security.audit import AuditRecord, audit_logger
        audit_rec = AuditRecord(
            event_type=clean_event_type,
            tenant_id=tenant_id or "unknown",
            user_id=user_id or "unknown",
            action=action,
            result=result,
            document_id=document_id,
            request_id=get_request_id(),
            details=sanitized_details,
        )
        audit_logger.log(audit_rec)
    except Exception:
        pass

    return log_record


def emit_operational_event(
    event: str,
    level: str = "INFO",
    duration_ms: Optional[float] = None,
    status: Optional[str] = None,
    error_class: Optional[str] = None,
    error_code: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    telemetry_model: Optional[SafeTelemetryBase] = None,
) -> Dict[str, Any]:
    """Emits a general operational event to structured logs."""
    return structured_logger.log_event(
        event=event,
        level=level,
        duration_ms=duration_ms,
        status=status,
        error_class=error_class,
        error_code=error_code,
        details=details,
        telemetry_model=telemetry_model,
    )
