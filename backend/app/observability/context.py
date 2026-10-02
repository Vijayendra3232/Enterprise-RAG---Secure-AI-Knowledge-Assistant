"""
context.py — Request and tracing correlation context propagation via ContextVars.
Propagates request_id, trace_id, span_id, and safe (HMAC-hashed) tenant/user IDs
across asynchronous and worker execution boundaries.
"""

import re
import uuid
from contextvars import ContextVar
from typing import Optional, Dict, Any

# Safe regex pattern for request IDs: 1 to 64 alphanumeric characters, underscores, or hyphens
REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# ContextVar definitions with default values
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_span_id_var: ContextVar[str] = ContextVar("span_id", default="")
_safe_tenant_id_var: ContextVar[str] = ContextVar("safe_tenant_id", default="")
_safe_user_id_var: ContextVar[str] = ContextVar("safe_user_id", default="")


def validate_or_generate_request_id(client_req_id: Optional[str]) -> str:
    """
    Validates client-provided request ID against allowed character set and length bounds.
    Generates a secure UUID-based request ID if invalid, malicious, or omitted.
    """
    if client_req_id and isinstance(client_req_id, str):
        sanitized = client_req_id.strip()
        if REQUEST_ID_PATTERN.match(sanitized):
            return sanitized
    return str(uuid.uuid4())


def generate_trace_id() -> str:
    """Generates a standard 16-byte (32-character hex) trace ID."""
    return uuid.uuid4().hex


def generate_span_id() -> str:
    """Generates a standard 8-byte (16-character hex) span ID."""
    return uuid.uuid4().hex[:16]


def set_request_context(
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    safe_tenant_id: Optional[str] = None,
    safe_user_id: Optional[str] = None,
) -> None:
    """Sets correlation context variables for the current execution context."""
    if request_id is not None:
        _request_id_var.set(validate_or_generate_request_id(request_id))
    if trace_id is not None:
        _trace_id_var.set(trace_id)
    if span_id is not None:
        _span_id_var.set(span_id)
    if safe_tenant_id is not None:
        _safe_tenant_id_var.set(safe_tenant_id)
    if safe_user_id is not None:
        _safe_user_id_var.set(safe_user_id)


def get_request_id() -> Optional[str]:
    """Returns the current request ID or None if unset."""
    return _request_id_var.get() or None


def get_trace_id() -> Optional[str]:
    """Returns the current trace ID or None if unset."""
    return _trace_id_var.get() or None


def get_span_id() -> Optional[str]:
    """Returns the current span ID or None if unset."""
    return _span_id_var.get() or None


def get_safe_tenant_id() -> Optional[str]:
    """Returns the safe HMAC-hashed tenant ID for the current context."""
    return _safe_tenant_id_var.get() or None


def get_safe_user_id() -> Optional[str]:
    """Returns the safe HMAC-hashed user ID for the current context."""
    return _safe_user_id_var.get() or None


def get_current_context() -> Dict[str, Optional[str]]:
    """Returns a dictionary copy of the current correlation context."""
    return {
        "request_id": get_request_id(),
        "trace_id": get_trace_id(),
        "span_id": get_span_id(),
        "safe_tenant_id": get_safe_tenant_id(),
        "safe_user_id": get_safe_user_id(),
    }


def clear_request_context() -> None:
    """Clears all correlation context variables."""
    _request_id_var.set("")
    _trace_id_var.set("")
    _span_id_var.set("")
    _safe_tenant_id_var.set("")
    _safe_user_id_var.set("")
