"""
worker.py — Standalone Distributed Worker and WorkerPool implementation.
Supports atomic task claiming, bounded exponential backoff retries,
stale task recovery, and specialized idempotent task dispatching.

PRODUCTION ENTRY POINT:
    python -m app.tasks.worker
"""

import sys
import time
import signal
import uuid
import logging
import threading
from typing import Optional, List, Dict, Any

from app import config
from app.storage.database import SessionLocal
from app.storage.search.factory import get_search_store
from app.storage.blob import get_document_storage
from app.connectors.secrets import get_secret_provider
from app.storage.models.task import Task
from app.tasks.queue import TaskQueueInterface, get_task_queue
from app.tasks.handlers.base import TaskHandler, WorkerContext, RetryableTaskError, NonRetryableTaskError
from app.tasks.handlers.ingestion import DocumentIngestHandler
from app.tasks.handlers.deletion import DocumentDeleteHandler
from app.tasks.handlers.permission_sync import PermissionSyncHandler
from app.tasks.handlers.connector_sync import ConnectorSyncHandler
from app.tasks.handlers.reindex import DocumentReindexHandler, IndexRebuildHandler

logger = logging.getLogger(__name__)


class WorkerRunner:
    """
    Single worker execution engine running task polling and execution loop.
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        task_queue: Optional[TaskQueueInterface] = None,
        handlers: Optional[List[TaskHandler]] = None,
        session_factory=SessionLocal,
        poll_interval: float = 1.0,
        stale_threshold_seconds: int = 300,
        base_retry_delay: int = 2,
        max_retry_delay: int = 60,
        rag_service: Optional[Any] = None,
    ):
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.task_queue = task_queue or get_task_queue()
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self.stale_threshold_seconds = stale_threshold_seconds
        self.base_retry_delay = base_retry_delay
        self.max_retry_delay = max_retry_delay
        self.rag_service = rag_service
        self._running = False
        self._last_stale_recovery = 0.0

        # Register default handlers if none provided
        self.handlers: List[TaskHandler] = handlers or [
            DocumentIngestHandler(),
            DocumentDeleteHandler(),
            PermissionSyncHandler(),
            ConnectorSyncHandler(),
            DocumentReindexHandler(),
            IndexRebuildHandler(),
        ]

    def _get_handler(self, task_type: str) -> Optional[TaskHandler]:
        for h in self.handlers:
            if h.can_handle(task_type):
                return h
        return None

    def calculate_backoff(self, attempt_count: int) -> int:
        """
        Calculate bounded exponential backoff delay in seconds:
        delay = base_retry_delay * 2^(attempt - 1), capped at max_retry_delay.
        """
        exponent = max(0, attempt_count - 1)
        delay = self.base_retry_delay * (2 ** exponent)
        return min(delay, self.max_retry_delay)

    def process_one(self) -> bool:
        """
        Poll and process a single task. Returns True if a task was processed, False otherwise.
        """
        task: Optional[Task] = self.task_queue.dequeue(worker_id=self.worker_id)
        if not task:
            return False

        import time
        from app.observability.tracing import tracer
        from app.observability.metrics import task_duration_seconds
        from app.observability.schemas import WorkerTelemetry
        from app.observability.logging import structured_logger
        from app.observability.context import set_request_context, clear_request_context
        from app.observability.redaction import SafeIdentityHasher

        t_start = time.perf_counter()
        safe_tenant = SafeIdentityHasher.hash_tenant_id(task.tenant_id)
        set_request_context(request_id=task.id, safe_tenant_id=safe_tenant)

        task_id_safe = SafeIdentityHasher.hash_identifier(task.id)

        structured_logger.info(
            "task.claimed",
            telemetry_model=WorkerTelemetry(
                task_type=task.task_type,
                task_id_hash=task_id_safe,
                attempt=task.attempt_count,
                status="claimed",
                duration_ms=0.0,
            ),
        )

        handler = self._get_handler(task.task_type)
        if not handler:
            err = f"No handler registered for task type '{task.task_type}'."
            structured_logger.error(
                "task.failed",
                error_class="NoHandlerRegistered",
                telemetry_model=WorkerTelemetry(
                    task_type=task.task_type,
                    task_id_hash=task_id_safe,
                    attempt=task.attempt_count,
                    status="failed",
                    duration_ms=0.0,
                    error_class="NoHandlerRegistered",
                ),
            )
            self.task_queue.fail(task.id, task.tenant_id, err)
            clear_request_context()
            return True

        db = self.session_factory()
        try:
            with tracer.start_span(
                f"worker.task.{task.task_type.lower()}",
                attributes={"task.type": task.task_type, "task.attempt": task.attempt_count},
            ) as span:
                context = WorkerContext(
                    db=db,
                    rag_service=self.rag_service,
                    worker_id=self.worker_id,
                )
                # Immediate early heartbeat right after task claim & context creation
                context.heartbeat(task.id)

                # Initialize dependencies with heartbeat coverage
                context.search_store = get_search_store()
                context.heartbeat(task.id)

                context.blob_storage = get_document_storage()
                context.secret_provider = get_secret_provider()
                context.heartbeat(task.id)

                result = handler.handle(task, context)
                duration_ms = (time.perf_counter() - t_start) * 1000
                task_duration_seconds.observe(duration_ms / 1000.0, labels={"task_type": task.task_type})

                self.task_queue.acknowledge(task.id, task.tenant_id, result)

                structured_logger.info(
                    "task.completed",
                    duration_ms=duration_ms,
                    telemetry_model=WorkerTelemetry(
                        task_type=task.task_type,
                        task_id_hash=task_id_safe,
                        attempt=task.attempt_count,
                        status="completed",
                        duration_ms=duration_ms,
                    ),
                )
                return True

        except RetryableTaskError as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000
            backoff = self.calculate_backoff(task.attempt_count)
            task_duration_seconds.observe(duration_ms / 1000.0, labels={"task_type": task.task_type})

            structured_logger.warning(
                "task.retrying",
                duration_ms=duration_ms,
                error_class=exc.__class__.__name__,
                telemetry_model=WorkerTelemetry(
                    task_type=task.task_type,
                    task_id_hash=task_id_safe,
                    attempt=task.attempt_count,
                    status="retrying",
                    duration_ms=duration_ms,
                    retry_delay_s=backoff,
                    error_class=exc.__class__.__name__,
                ),
            )
            self.task_queue.retry(task.id, task.tenant_id, str(exc), backoff_seconds=backoff)
            return True

        except NonRetryableTaskError as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000
            task_duration_seconds.observe(duration_ms / 1000.0, labels={"task_type": task.task_type})

            structured_logger.error(
                "task.failed",
                duration_ms=duration_ms,
                error_class=exc.__class__.__name__,
                telemetry_model=WorkerTelemetry(
                    task_type=task.task_type,
                    task_id_hash=task_id_safe,
                    attempt=task.attempt_count,
                    status="failed",
                    duration_ms=duration_ms,
                    error_class=exc.__class__.__name__,
                ),
            )
            self.task_queue.fail(task.id, task.tenant_id, str(exc))
            return True

        except Exception as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000
            backoff = self.calculate_backoff(task.attempt_count)
            task_duration_seconds.observe(duration_ms / 1000.0, labels={"task_type": task.task_type})

            structured_logger.error(
                "task.failed",
                duration_ms=duration_ms,
                error_class=exc.__class__.__name__,
                telemetry_model=WorkerTelemetry(
                    task_type=task.task_type,
                    task_id_hash=task_id_safe,
                    attempt=task.attempt_count,
                    status="failed",
                    duration_ms=duration_ms,
                    retry_delay_s=backoff,
                    error_class=exc.__class__.__name__,
                ),
            )
            self.task_queue.retry(task.id, task.tenant_id, f"Unexpected error: {exc}", backoff_seconds=backoff)
            return True

        finally:
            clear_request_context()
            db.close()

    def run_stale_recovery_if_due(self) -> None:
        now = time.time()
        if now - self._last_stale_recovery > 60.0:
            self._last_stale_recovery = now
            try:
                recovered = self.task_queue.recover_stale(self.stale_threshold_seconds)
                if recovered > 0:
                    logger.info(f"[{self.worker_id}] Stale task recovery restored {recovered} task(s).")
            except Exception as exc:
                logger.error(f"[{self.worker_id}] Stale task recovery error: {exc}")

    def run_loop(self) -> None:
        """Run continuous worker polling loop until stopped."""
        self._running = True
        logger.info(f"[{self.worker_id}] Worker loop started (poll_interval={self.poll_interval}s).")
        while self._running:
            try:
                self.run_stale_recovery_if_due()
                did_work = self.process_one()
                if not did_work:
                    time.sleep(self.poll_interval)
            except Exception as exc:
                logger.error(f"[{self.worker_id}] Loop iteration exception: {exc}")
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False


class WorkerPool:
    """
    Manages a pool of concurrent WorkerRunner instances.
    """

    def __init__(
        self,
        concurrency: int = 2,
        task_queue: Optional[TaskQueueInterface] = None,
        poll_interval: float = 1.0,
        rag_service: Optional[Any] = None,
    ):
        self.concurrency = max(1, concurrency)
        self.task_queue = task_queue or get_task_queue()
        self.poll_interval = poll_interval
        self.rag_service = rag_service
        self.workers: List[WorkerRunner] = []
        self.threads: List[threading.Thread] = []
        self._running = False

    def start(self) -> None:
        """Start all worker threads."""
        if self._running:
            return
        self._running = True
        self.workers = []
        self.threads = []

        for i in range(self.concurrency):
            runner = WorkerRunner(
                worker_id=f"worker-{i+1}",
                task_queue=self.task_queue,
                poll_interval=self.poll_interval,
                rag_service=self.rag_service,
            )
            thread = threading.Thread(
                target=runner.run_loop,
                name=f"worker-thread-{i+1}",
                daemon=True,
            )
            self.workers.append(runner)
            self.threads.append(thread)
            thread.start()

        logger.info(f"[WorkerPool] Started {self.concurrency} concurrent worker(s).")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop all workers gracefully."""
        self._running = False
        for w in self.workers:
            w.stop()
        for t in self.threads:
            t.join(timeout=timeout)
        logger.info("[WorkerPool] All workers stopped.")


def run_standalone_worker() -> None:
    """
    Production entry point for dedicated worker container/process.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )
    logger.info("==================================================================")
    logger.info("STARTING STANDALONE ENTERPRISE RAG ASYNCHRONOUS WORKER PROCESS")
    logger.info("==================================================================")

    concurrency = getattr(config, "WORKER_CONCURRENCY", 2)
    pool = WorkerPool(concurrency=concurrency)

    def handle_signal(sig, frame):
        logger.info("[Worker] Received termination signal. Shutting down gracefully...")
        pool.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    pool.start()

    # Keep main process alive
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pool.stop()


if __name__ == "__main__":
    run_standalone_worker()
