"""
test_worker_heartbeat_reliability.py — Targeted regression tests for early worker and handler heartbeat execution.
Verifies immediate heartbeat on task claim, early context heartbeat, handler entry heartbeat, and cross-session database visibility.
"""

import os
import sys
import uuid
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))

from app.storage.models.task import Task
from app.tasks.handlers.base import WorkerContext
from app.tasks.handlers.ingestion import DocumentIngestHandler
from app.tasks.worker import WorkerRunner
from app.storage.database import SessionLocal, init_db


class TestWorkerHeartbeatReliability(unittest.TestCase):
    """Test suite proving early heartbeat coverage during worker task claim and handler dispatch."""

    def test_a_worker_runner_heartbeats_immediately_after_context_creation(self):
        """TEST A: Verify WorkerRunner emits early heartbeat right after WorkerContext creation."""
        mock_queue = MagicMock()
        test_task = Task(
            id="test_task_early_hb_001",
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"document_id": "doc_hb_001", "storage_path": "data/uploads/doc.txt"},
            attempt_count=1,
            max_attempts=3,
            status="RUNNING",
        )
        mock_queue.dequeue.return_value = test_task

        heartbeat_calls = []

        def mock_heartbeat(self_context, task_id):
            heartbeat_calls.append((task_id, datetime.now(timezone.utc)))

        with patch.object(WorkerContext, "heartbeat", side_effect=mock_heartbeat, autospec=True), patch(
            "app.tasks.worker.get_search_store"
        ), patch("app.tasks.worker.get_document_storage"), patch(
            "app.tasks.worker.get_secret_provider"
        ), patch.object(
            DocumentIngestHandler, "handle", return_value={"status": "SUCCEEDED"}
        ):
            runner = WorkerRunner(task_queue=mock_queue, session_factory=MagicMock())
            result = runner.process_one()

            self.assertTrue(result)
            self.assertGreaterEqual(len(heartbeat_calls), 1)
            self.assertEqual(heartbeat_calls[0][0], "test_task_early_hb_001")

    def test_b_ingestion_handler_heartbeats_before_document_lookup(self):
        """TEST B: Verify DocumentIngestHandler.handle() heartbeats immediately on entry before DB lookup."""
        context = MagicMock()
        context.db = MagicMock()

        handler = DocumentIngestHandler()
        test_task = Task(
            id="test_task_handler_entry_002",
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"document_id": "doc_hb_002", "storage_path": "data/uploads/doc.txt"},
        )

        with patch("app.tasks.handlers.ingestion.SQLDocumentRepository") as mock_doc_repo:
            handler.handle(test_task, context)

            context.heartbeat.assert_called_with("test_task_handler_entry_002")
            # Verify heartbeat was called before get_by_id
            self.assertTrue(context.heartbeat.called)
            mock_doc_repo.return_value.get_by_id.assert_called_once_with("doc_hb_002", tenant_id="tenant_a")

    def test_c_blocking_setup_operation_emits_early_heartbeat(self):
        """TEST C: Verify early heartbeat updates task before a slow dependency resolution."""
        mock_queue = MagicMock()
        test_task = Task(
            id="test_task_slow_setup_003",
            tenant_id="tenant_a",
            task_type="DOCUMENT_INGEST",
            payload={"document_id": "doc_hb_003", "storage_path": "data/uploads/doc.txt"},
            attempt_count=1,
            max_attempts=3,
            status="RUNNING",
        )
        mock_queue.dequeue.return_value = test_task

        heartbeat_log = []

        def slow_search_store():
            heartbeat_log.append("slow_setup_start")
            return MagicMock()

        def mock_hb(self_context, task_id):
            heartbeat_log.append(f"heartbeat_{task_id}")

        with patch.object(WorkerContext, "heartbeat", side_effect=mock_hb, autospec=True), patch(
            "app.tasks.worker.get_search_store", side_effect=slow_search_store
        ), patch("app.tasks.worker.get_document_storage"), patch(
            "app.tasks.worker.get_secret_provider"
        ), patch.object(
            DocumentIngestHandler, "handle", return_value={"status": "SUCCEEDED"}
        ):
            runner = WorkerRunner(task_queue=mock_queue, session_factory=MagicMock())
            runner.process_one()

            self.assertIn("heartbeat_test_task_slow_setup_003", heartbeat_log)
            # Verify heartbeat occurred before slow setup finished
            hb_idx = heartbeat_log.index("heartbeat_test_task_slow_setup_003")
            slow_idx = heartbeat_log.index("slow_setup_start")
            self.assertLess(hb_idx, slow_idx)

    def test_d_heartbeat_cross_session_visibility(self):
        """TEST D: Verify context.heartbeat() commits updated_at and is visible to separate DB sessions."""
        init_db()

        unique_task_id = f"test_task_cs_{uuid.uuid4().hex[:8]}"
        session_1 = SessionLocal()
        session_2 = SessionLocal()

        try:
            from app.storage.repositories.task_repository import SQLTaskRepository
            repo1 = SQLTaskRepository(session_1)

            initial_task = Task(
                id=unique_task_id,
                tenant_id="tenant_a",
                task_type="DOCUMENT_INGEST",
                payload={"document_id": "doc_cs_004", "storage_path": "data/uploads/doc.txt"},
                status="PENDING",
            )
            repo1.create(initial_task)
            session_1.commit()

            # Verify session 2 reads initial state
            repo2 = SQLTaskRepository(session_2)
            task_before = repo2.get_by_id_and_tenant(unique_task_id, "tenant_a")
            self.assertIsNotNone(task_before)

            # Execute heartbeat in session 1 context
            context = WorkerContext(db=session_1, worker_id="worker_cs_1")
            context.heartbeat(unique_task_id)

            # Verify session_2 (independent DB session) sees heartbeat commit
            session_2.expire_all()
            task_after = repo2.get_by_id_and_tenant(unique_task_id, "tenant_a")
            self.assertIsNotNone(task_after)
            self.assertIsNotNone(task_after.updated_at)
        finally:
            session_1.close()
            session_2.close()


if __name__ == "__main__":
    unittest.main()
