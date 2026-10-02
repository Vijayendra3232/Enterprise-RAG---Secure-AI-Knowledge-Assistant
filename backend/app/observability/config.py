"""
config.py — Configuration dataclass and loaders for production observability.
"""

from dataclasses import dataclass
from typing import Optional
from app import config


@dataclass(frozen=True)
class ObservabilityConfig:
    log_level: str = "INFO"
    log_format: str = "json"
    enable_tracing: bool = True
    trace_sample_rate: float = 1.0
    enable_metrics: bool = True
    monitoring_api_key: str = ""
    telemetry_hmac_key: str = ""
    slow_request_threshold_ms: int = 2000
    slow_db_threshold_ms: int = 200
    slow_search_threshold_ms: int = 500
    slow_llm_threshold_ms: int = 3000
    environment: str = "development"


def get_observability_config() -> ObservabilityConfig:
    """Returns a snapshot of the current observability configuration."""
    return ObservabilityConfig(
        log_level=getattr(config, "LOG_LEVEL", "INFO").upper(),
        log_format=getattr(config, "LOG_FORMAT", "json").lower(),
        enable_tracing=getattr(config, "ENABLE_TRACING", True),
        trace_sample_rate=float(getattr(config, "TRACE_SAMPLE_RATE", 1.0)),
        enable_metrics=getattr(config, "ENABLE_METRICS", True),
        monitoring_api_key=getattr(config, "MONITORING_API_KEY", ""),
        telemetry_hmac_key=getattr(config, "TELEMETRY_HMAC_KEY", ""),
        slow_request_threshold_ms=int(getattr(config, "SLOW_REQUEST_THRESHOLD_MS", 2000)),
        slow_db_threshold_ms=int(getattr(config, "SLOW_DB_THRESHOLD_MS", 200)),
        slow_search_threshold_ms=int(getattr(config, "SLOW_SEARCH_THRESHOLD_MS", 500)),
        slow_llm_threshold_ms=int(getattr(config, "SLOW_LLM_THRESHOLD_MS", 3000)),
        environment=getattr(config, "ENVIRONMENT", "development").lower(),
    )
