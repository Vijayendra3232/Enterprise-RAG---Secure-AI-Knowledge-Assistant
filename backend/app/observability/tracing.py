"""
tracing.py — Production-extensible distributed tracing subsystem.
Provides in-process and distributed span creation, context propagation,
bounded async batching, sampling, and non-blocking exporter failure isolation.
"""

import time
import uuid
import random
import threading
import queue
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

from app import config
from app.observability.context import (
    get_trace_id,
    get_span_id,
    generate_span_id,
    _span_id_var,
    _trace_id_var,
)
from app.observability.redaction import redact_data


class Span:
    """
    Represents an isolated unit of execution within a distributed trace.
    Attributes contain allowlisted safe metadata only (no document text, prompts, or secrets).
    """

    def __init__(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.start_time = time.perf_counter()
        self.end_time: Optional[float] = None
        self.duration_ms: Optional[float] = None
        self.status = "OK"  # OK or ERROR
        self.attributes: Dict[str, Any] = {}

        if attributes:
            for k, v in attributes.items():
                self.set_attribute(k, v)

    def set_attribute(self, key: str, value: Any) -> None:
        """Sets a safe primitive attribute on the span (strings, numbers, bools)."""
        if isinstance(value, (str, int, float, bool)):
            # Redact string attributes for defense-in-depth
            if isinstance(value, str):
                self.attributes[key] = redact_data(value)
            else:
                self.attributes[key] = value

    def set_status(self, status: str) -> None:
        """Sets the span status ('OK' or 'ERROR')."""
        self.status = "ERROR" if status.upper() == "ERROR" else "OK"

    def end(self) -> None:
        """Ends the span and computes monotonic duration in milliseconds."""
        if self.end_time is None:
            self.end_time = time.perf_counter()
            self.duration_ms = round((self.end_time - self.start_time) * 1000, 3)

    def to_dict(self) -> Dict[str, Any]:
        """Serializes span metadata to dictionary."""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
        }


class SpanExporterInterface(ABC):
    """Abstract interface for span exporters."""

    @abstractmethod
    def export(self, spans: List[Span]) -> bool:
        """Exports a batch of completed spans. Returns True on success, False otherwise."""
        pass

    @abstractmethod
    def flush(self) -> None:
        """Flushes any buffered spans."""
        pass

    @abstractmethod
    def shutdown(self) -> None:
        """Shuts down the exporter and releases resources."""
        pass


class InMemorySpanExporter(SpanExporterInterface):
    """Bounded in-memory exporter used for dev/testing and trace verification."""

    def __init__(self, max_spans: int = 1000):
        self.max_spans = max_spans
        self.spans: List[Span] = []
        self._lock = threading.Lock()

    def export(self, spans: List[Span]) -> bool:
        with self._lock:
            self.spans.extend(spans)
            if len(self.spans) > self.max_spans:
                self.spans = self.spans[-self.max_spans:]
        return True

    def get_spans(self, name: Optional[str] = None) -> List[Span]:
        with self._lock:
            if name:
                return [s for s in self.spans if s.name == name]
            return list(self.spans)

    def clear(self) -> None:
        with self._lock:
            self.spans.clear()

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        self.clear()


class AsyncBatchSpanExporter(SpanExporterInterface):
    """
    Production-ready asynchronous batch exporter.
    Decouples span export from request processing using a bounded background queue,
    worker thread, batching, timeout protection, and non-blocking failure isolation.
    """

    def __init__(
        self,
        delegate: Optional[SpanExporterInterface] = None,
        max_queue_size: int = 5000,
        batch_size: int = 100,
        flush_interval_seconds: float = 1.0,
        flush_interval: Optional[float] = None,
    ):
        self.delegate = delegate or InMemorySpanExporter()
        self.max_queue_size = max_queue_size
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval if flush_interval is not None else flush_interval_seconds
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._dropped_spans_count = 0
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, name="tracer-async-exporter", daemon=True)
        self._thread.start()

    def _flush_batch(self, batch: List[Span]) -> None:
        self.delegate.export(batch)

    def export(self, spans: Any) -> bool:
        if isinstance(spans, Span):
            spans = [spans]
        for span in spans:
            try:
                self._queue.put_nowait(span)
            except queue.Full:
                self._dropped_spans_count += 1
        return True

    def _worker_loop(self) -> None:
        while self._running:
            batch: List[Span] = []
            try:
                # Wait for at least one span
                span = self._queue.get(timeout=self.flush_interval_seconds)
                batch.append(span)
                # Pull additional available spans up to batch_size
                while len(batch) < self.batch_size and not self._queue.empty():
                    batch.append(self._queue.get_nowait())
            except (queue.Empty, Exception):
                pass

            if batch:
                try:
                    self._flush_batch(batch)
                except Exception:
                    # Isolated failure: telemetry exporter failure must NEVER break the application
                    pass

    def flush(self) -> None:
        batch: List[Span] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            try:
                self._flush_batch(batch)
            except Exception:
                pass
        self.delegate.flush()

    def shutdown(self, timeout: float = 1.0) -> None:
        self._running = False
        self.flush()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.delegate.shutdown()


class Tracer:
    """
    Central Tracer coordinating span creation, sampling, and exporter dispatch.
    """

    def __init__(
        self,
        service_name: str = "enterprise-rag-api",
        exporter: Optional[SpanExporterInterface] = None,
        sample_rate: Optional[float] = None,
    ):
        self.service_name = service_name
        self.exporters: List[SpanExporterInterface] = [exporter] if exporter else [InMemorySpanExporter()]
        self.sample_rate = sample_rate

    def register_exporter(self, exporter: SpanExporterInterface) -> None:
        """Registers a span exporter."""
        self.exporters.append(exporter)

    def set_exporters(self, exporters: List[SpanExporterInterface]) -> None:
        """Replaces current exporter list."""
        self.exporters = list(exporters)

    def is_sampled(self) -> bool:
        """Evaluates whether the current trace should be sampled based on TRACE_SAMPLE_RATE."""
        rate = self.sample_rate if self.sample_rate is not None else float(getattr(config, "TRACE_SAMPLE_RATE", 1.0))
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return random.random() < rate

    @contextmanager
    def start_span(
        self,
        name: str,
        parent_span_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        """
        Context manager that creates, activates, and finishes a trace span.
        Propagates trace_id and span_id across nested execution.
        """
        enabled = getattr(config, "ENABLE_TRACING", True)
        if not enabled or not self.is_sampled():
            yield None
            return

        trace_id = get_trace_id()
        parent_id = parent_span_id or get_span_id()
        span_id = generate_span_id()

        span = Span(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_id if parent_id != span_id else None,
            attributes=attributes,
        )

        # Set new current span in ContextVar
        token = _span_id_var.set(span_id)
        try:
            yield span
        except Exception as exc:
            span.set_status("ERROR")
            span.set_attribute("error.class", exc.__class__.__name__)
            raise
        finally:
            span.end()
            _span_id_var.reset(token)
            # Dispatch to exporters with failure isolation
            for exporter in self.exporters:
                try:
                    exporter.export([span])
                except Exception:
                    pass

    # Alias for start_span
    span = start_span


# Global tracer instance
tracer = Tracer()
