"""
errors.py — Error taxonomy and safe error sanitization.
Categorizes internal errors into a standardized taxonomy while guaranteeing
that internal stack traces, credentials, document content, and secrets
are never exposed to API clients.
"""

from enum import Enum
from typing import Dict, Any, Optional
from app.observability.context import get_request_id


class ErrorTaxonomy(str, Enum):
    CLIENT_ERROR = "CLIENT_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_DEPENDENCY_ERROR = "TRANSIENT_DEPENDENCY_ERROR"
    PERMANENT_DEPENDENCY_ERROR = "PERMANENT_DEPENDENCY_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    SEARCH_ERROR = "SEARCH_ERROR"
    LLM_ERROR = "LLM_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    CONNECTOR_ERROR = "CONNECTOR_ERROR"
    TASK_ERROR = "TASK_ERROR"
    SECURITY_ERROR = "SECURITY_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def classify_error(exc: Exception) -> ErrorTaxonomy:
    """Classifies an arbitrary exception into a standardized internal ErrorTaxonomy value."""
    exc_name = exc.__class__.__name__.lower()
    exc_str = str(exc).lower()

    if "jwt" in exc_name or "auth" in exc_name or "token" in exc_name:
        if "forbidden" in exc_str or "unauthorized" in exc_str:
            return ErrorTaxonomy.AUTHENTICATION_ERROR
        return ErrorTaxonomy.AUTHENTICATION_ERROR

    if "permission" in exc_name or "accessdenied" in exc_name or "unauthorized" in exc_name:
        return ErrorTaxonomy.AUTHORIZATION_ERROR

    if "ssrf" in exc_name or "security" in exc_name or "tenantmismatch" in exc_name:
        return ErrorTaxonomy.SECURITY_ERROR

    if "notfound" in exc_name or "404" in exc_str:
        return ErrorTaxonomy.NOT_FOUND

    if "validation" in exc_name or "valueerror" in exc_name:
        return ErrorTaxonomy.VALIDATION_ERROR

    if "ratelimit" in exc_name or "429" in exc_str or "throttle" in exc_str:
        return ErrorTaxonomy.RATE_LIMITED

    if "database" in exc_name or "sql" in exc_name or "operationalerror" in exc_name or "integrityerror" in exc_name:
        return ErrorTaxonomy.DATABASE_ERROR

    if "opensearch" in exc_name or "elasticsearch" in exc_name or "search" in exc_name:
        return ErrorTaxonomy.SEARCH_ERROR

    if "groq" in exc_name or "openai" in exc_name or "llm" in exc_name:
        return ErrorTaxonomy.LLM_ERROR

    if "s3" in exc_name or "storage" in exc_name or "blob" in exc_name:
        return ErrorTaxonomy.STORAGE_ERROR

    if "connector" in exc_name:
        return ErrorTaxonomy.CONNECTOR_ERROR

    if "task" in exc_name or "worker" in exc_name:
        return ErrorTaxonomy.TASK_ERROR

    if "timeout" in exc_name or "connectionerror" in exc_name:
        return ErrorTaxonomy.TRANSIENT_DEPENDENCY_ERROR

    return ErrorTaxonomy.INTERNAL_ERROR


def sanitize_error_detail(exc: Exception, request_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Constructs a safe, sanitized client-facing error payload.
    Excludes internal stack traces, database parameters, and secret values.
    """
    taxonomy = classify_error(exc)
    req_id = request_id or get_request_id()

    # Safe user-facing message mapping
    if taxonomy == ErrorTaxonomy.AUTHENTICATION_ERROR:
        safe_msg = "Authentication failed or token is invalid."
    elif taxonomy == ErrorTaxonomy.AUTHORIZATION_ERROR:
        safe_msg = "Access denied by enterprise authorization policy."
    elif taxonomy == ErrorTaxonomy.SECURITY_ERROR:
        safe_msg = "Request rejected due to security policy violation."
    elif taxonomy == ErrorTaxonomy.NOT_FOUND:
        safe_msg = "Requested resource was not found."
    elif taxonomy == ErrorTaxonomy.RATE_LIMITED:
        safe_msg = "Rate limit exceeded. Please retry later."
    elif taxonomy == ErrorTaxonomy.VALIDATION_ERROR:
        safe_msg = "Invalid request parameters."
    elif taxonomy in {ErrorTaxonomy.DATABASE_ERROR, ErrorTaxonomy.SEARCH_ERROR, ErrorTaxonomy.STORAGE_ERROR, ErrorTaxonomy.LLM_ERROR}:
        safe_msg = "A backend service error occurred while processing the request."
    else:
        safe_msg = "An unexpected error occurred."

    return {
        "detail": safe_msg,
        "error_code": taxonomy.value,
        "request_id": req_id,
    }
