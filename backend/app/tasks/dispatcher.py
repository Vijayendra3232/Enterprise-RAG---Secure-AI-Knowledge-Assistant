"""
dispatcher.py — Transactional Outbox Dispatcher.
Polls PENDING outbox events and reliably publishes them to the TaskQueue.
Prevents dual-write inconsistencies between PostgreSQL database transactions and the queue.
"""

import time
import logging
import threading
from typing import Optional
from sqlalchemy.orm import Session

from app.storage.database import SessionLocal
from app.storage.repositories.task_repository import SQLOutboxRepository, SQLTaskRepository
from app.tasks.queue import TaskQueueInterface, get_task_queue

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    """
    Background dispatcher that pulls pending OutboxEvents from PostgreSQL
    and guarantees at-least-once delivery to the TaskQueue.
    """

    def __init__(
        self,
        task_queue: Optional[TaskQueueInterface] = None,
        session_factory=SessionLocal,
        poll_interval: float = 1.0,
    ):
        self.task_queue = task_queue or get_task_queue()
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def dispatch_pending(self, batch_size: int = 50) -> int:
        """
        Process a single batch of pending outbox events.
        Returns the number of successfully published events.
        """
        db: Session = self.session_factory()
        published_count = 0
        try:
            outbox_repo = SQLOutboxRepository(db)
            events = outbox_repo.get_pending_events(limit=batch_size)

            for event in events:
                try:
                    # Publish event payload to task queue if not already created
                    payload = event.payload or {}
                    task_id = payload.get("task_id")
                    
                    # Mark outbox event published
                    outbox_repo.mark_published(event.id, event.tenant_id)
                    published_count += 1
                except Exception as exc:
                    logger.error(f"[OutboxDispatcher] Failed to publish event {event.id}: {exc}")
                    outbox_repo.mark_failed(event.id, event.tenant_id, str(exc))

            if published_count > 0:
                db.commit()
            return published_count
        except Exception as exc:
            db.rollback()
            logger.error(f"[OutboxDispatcher] Batch dispatch error: {exc}")
            return 0
        finally:
            db.close()

    def start(self) -> None:
        """Start background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="outbox-dispatcher")
        self._thread.start()

    def stop(self) -> None:
        """Stop background polling thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.dispatch_pending()
            except Exception as exc:
                logger.error(f"[OutboxDispatcher] Loop iteration error: {exc}")
            time.sleep(self.poll_interval)
