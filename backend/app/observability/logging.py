"""
logging.py — Centralized structured JSON logging with anti-injection and defense-in-depth sanitization.
Guarantees consistent, machine-readable log lines with correlation context,
while strictly excluding document content, queries, prompts, and credentials.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from app import config
from app.observability.context import (
    get_request_id,
    get_trace_id,
    get_span_id,
    get_safe_tenant_id,
    get_safe_user_id,
)
from app.observability.redaction import redact_data
from app.observability.schemas import LogRecordSchema, SafeTelemetryBase


def sanitize_string_for_logging(value: str, max_len: int = 256) -> str:
    """Sanitizes strings to prevent log injection by replacing newlines, carriage returns, and control chars."""
    if not isinstance(value, str):
        value = str(value)
    # Strip carriage returns and newlines
    sanitized = value.replace("\r", " ").replace("\n", " ").strip()
    # Filter non-printable / control characters
    sanitized = "".join(ch for ch in sanitized if ch.isprintable())
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "..."
    return sanitized


class StructuredLogger:
    """
    Centralized structured logger outputting JSON in production and capturing
    in-memory logs for diagnostic assertions and test suites.
    """

    def __init__(self, service_name: str = "enterprise-rag-api", max_history: int = 1000):
        self.service_name = service_name
        self.max_history = max_history
        self._history: List[Dict[str, Any]] = []

    def log_event(
        self,
        event: str,
        level: str = "INFO",
        duration_ms: Optional[float] = None,
        status: Optional[str] = None,
        error_class: Optional[str] = None,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        telemetry_model: Optional[SafeTelemetryBase] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Emits a structured log event with correlation identifiers.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        env = getattr(config, "ENVIRONMENT", "development").lower()
        log_format = getattr(config, "LOG_FORMAT", "json").lower()

        # Extract details from telemetry model if provided
        final_details: Dict[str, Any] = {}
        if telemetry_model:
            final_details.update(telemetry_model.model_dump(exclude_unset=True))
        if details:
            redacted_details = redact_data(details)
            if isinstance(redacted_details, dict):
                final_details.update(redacted_details)

        final_extra: Dict[str, Any] = {}
        if "extra" in kwargs:
            extra_val = kwargs.pop("extra")
            if isinstance(extra_val, dict):
                redacted_extra = redact_data(extra_val)
                if isinstance(redacted_extra, dict):
                    final_extra.update(redacted_extra)
                    final_details.update(redacted_extra)
        if kwargs:
            redacted_kw = redact_data(kwargs)
            if isinstance(redacted_kw, dict):
                final_details.update(redacted_kw)

        # Sanitize event name and safe identifiers
        clean_event = sanitize_string_for_logging(event, max_len=512)
        raw_req_id = get_request_id()
        raw_trace_id = get_trace_id()
        raw_span_id = get_span_id()
        raw_safe_tenant = get_safe_tenant_id()
        raw_safe_user = get_safe_user_id()

        clean_req_id = sanitize_string_for_logging(raw_req_id, max_len=64) if raw_req_id else None
        clean_trace_id = sanitize_string_for_logging(raw_trace_id, max_len=64) if raw_trace_id else None
        clean_span_id = sanitize_string_for_logging(raw_span_id, max_len=32) if raw_span_id else None
        clean_safe_tenant = sanitize_string_for_logging(raw_safe_tenant, max_len=64) if raw_safe_tenant else None
        clean_safe_user = sanitize_string_for_logging(raw_safe_user, max_len=64) if raw_safe_user else None

        record = LogRecordSchema(
            timestamp=timestamp,
            level=level.upper(),
            logger=self.service_name,
            service=self.service_name,
            environment=env,
            event=clean_event,
            message=clean_event,
            request_id=clean_req_id,
            trace_id=clean_trace_id,
            span_id=clean_span_id,
            safe_tenant_id=clean_safe_tenant,
            safe_user_id=clean_safe_user,
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            status=sanitize_string_for_logging(status, max_len=32) if status else None,
            error_class=sanitize_string_for_logging(error_class, max_len=64) if error_class else None,
            error_code=sanitize_string_for_logging(error_code, max_len=64) if error_code else None,
            details=final_details if final_details else None,
            extra=final_extra if final_extra else None,
        )

        record_dict = record.model_dump(exclude_none=True)

        # Append to in-memory test trace
        self._history.append(record_dict)
        if len(self._history) > self.max_history:
            self._history.pop(0)

        # Output to stdout/stderr
        try:
            if log_format == "json":
                out = json.dumps(record_dict)
                sys.stdout.write(out + "\n")
            else:
                out = f"[{record.timestamp}] [{record.level}] [{record.event}] req={record.request_id} dur={record.duration_ms}ms"
                sys.stdout.write(out + "\n")
            sys.stdout.flush()
        except Exception:
            pass

        return record_dict

    def info(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, level="INFO", **kwargs)

    def warning(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, level="WARNING", **kwargs)

    def error(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, level="ERROR", **kwargs)

    def debug(self, event: str, **kwargs) -> Dict[str, Any]:
        return self.log_event(event=event, level="DEBUG", **kwargs)

    def get_recent_logs(self, event: Optional[str] = None, level: Optional[str] = None) -> List[Dict[str, Any]]:
        logs = self._history
        if event:
            logs = [entry for entry in logs if entry.get("event") == event]
        if level:
            logs = [entry for entry in logs if entry.get("level") == level.upper()]
        return logs

    def clear_logs(self) -> None:
        self._history.clear()


# Global structured logger instance
structured_logger = StructuredLogger()
