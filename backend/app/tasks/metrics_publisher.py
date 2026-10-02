"""
metrics_publisher.py — Independent Task Queue Metrics Publisher for ECS Autoscaling.
Runs as a decoupled service (rag-metrics-publisher) querying PostgreSQL and emitting
CloudWatch custom metrics to drive worker autoscaling even if worker processes crash.
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from sqlalchemy import text

from app import config
from app.storage.database import SessionLocal

logger = logging.getLogger("app.tasks.metrics_publisher")


class QueueMetricsPublisher:
    """
    Independently polls the PostgreSQL task queue and publishes metrics to CloudWatch.
    """

    def __init__(
        self,
        namespace: str = "EnterpriseRAG/Tasks",
        environment: Optional[str] = None,
        poll_interval_seconds: float = 15.0,
        cloudwatch_client: Optional[Any] = None,
    ):
        self.namespace = namespace
        self.environment = environment or getattr(config, "ENVIRONMENT", "development")
        self.poll_interval_seconds = poll_interval_seconds
        self._cw_client = cloudwatch_client
        self._running = False

    def get_cloudwatch_client(self) -> Optional[Any]:
        if self._cw_client is not None:
            return self._cw_client
        try:
            import boto3
            region = getattr(config, "AWS_REGION", os.getenv("AWS_REGION", "us-east-1"))
            self._cw_client = boto3.client("cloudwatch", region_name=region)
            return self._cw_client
        except Exception as e:
            logger.debug(f"[QueueMetricsPublisher] CloudWatch client unavailable: {e}")
            return None

    def collect_queue_metrics(self) -> Dict[str, Any]:
        """
        Queries PostgreSQL for queue depth, oldest task age, active tasks, and failures.
        Safe against empty tables and transient connection hiccups.
        """
        metrics = {
            "queue_depth": 0,
            "oldest_task_age_seconds": 0.0,
            "active_tasks": 0,
            "failed_tasks": 0,
            "by_type": {},
        }

        db = SessionLocal()
        try:
            # 1. Total pending queue depth and oldest pending task age
            pending_query = text(
                """
                SELECT
                    COUNT(*) as queue_depth,
                    COALESCE(MAX(EXTRACT(EPOCH FROM (NOW() - created_at))), 0.0) as oldest_age
                FROM tasks
                WHERE status = 'PENDING'
                """
            )
            res = db.execute(pending_query).fetchone()
            if res:
                metrics["queue_depth"] = int(res[0] or 0)
                metrics["oldest_task_age_seconds"] = float(res[1] or 0.0)

            # 2. Running (claimed) active tasks
            running_query = text(
                """
                SELECT COUNT(*) FROM tasks WHERE status = 'RUNNING'
                """
            )
            res_running = db.execute(running_query).scalar()
            metrics["active_tasks"] = int(res_running or 0)

            # 3. Failed tasks
            failed_query = text(
                """
                SELECT COUNT(*) FROM tasks WHERE status = 'FAILED'
                """
            )
            res_failed = db.execute(failed_query).scalar()
            metrics["failed_tasks"] = int(res_failed or 0)

            # 4. Queue depth by task_type (bounded cardinality)
            type_query = text(
                """
                SELECT task_type, COUNT(*)
                FROM tasks
                WHERE status = 'PENDING'
                GROUP BY task_type
                """
            )
            for row in db.execute(type_query).fetchall():
                task_type, count = row[0], row[1]
                if task_type:
                    metrics["by_type"][str(task_type)] = int(count)

        except Exception as e:
            logger.warning(f"[QueueMetricsPublisher] Database query error: {e}")
        finally:
            db.close()

        return metrics

    def publish_metrics_to_cloudwatch(self, metrics: Dict[str, Any]) -> bool:
        """
        Formats metrics into OpenMetrics / CloudWatch MetricData and sends them.
        Uses strictly bounded dimensions (Environment, TaskType).
        """
        cw = self.get_cloudwatch_client()
        if not cw:
            logger.debug("[QueueMetricsPublisher] Skipping CloudWatch put: client not initialized.")
            return False

        now = datetime.now(timezone.utc)
        env_dim = {"Name": "Environment", "Value": self.environment}

        metric_data: List[Dict[str, Any]] = [
            {
                "MetricName": "rag_task_queue_depth",
                "Dimensions": [env_dim, {"Name": "TaskType", "Value": "ALL"}],
                "Timestamp": now,
                "Value": float(metrics["queue_depth"]),
                "Unit": "Count",
            },
            {
                "MetricName": "rag_task_oldest_age_seconds",
                "Dimensions": [env_dim, {"Name": "TaskType", "Value": "ALL"}],
                "Timestamp": now,
                "Value": float(metrics["oldest_task_age_seconds"]),
                "Unit": "Seconds",
            },
            {
                "MetricName": "rag_worker_active_tasks",
                "Dimensions": [env_dim, {"Name": "TaskType", "Value": "ALL"}],
                "Timestamp": now,
                "Value": float(metrics["active_tasks"]),
                "Unit": "Count",
            },
            {
                "MetricName": "rag_task_failures",
                "Dimensions": [env_dim, {"Name": "TaskType", "Value": "ALL"}],
                "Timestamp": now,
                "Value": float(metrics["failed_tasks"]),
                "Unit": "Count",
            },
        ]

        # Add per-type queue depths
        for t_type, count in metrics.get("by_type", {}).items():
            clean_type = str(t_type)[:32]
            metric_data.append(
                {
                    "MetricName": "rag_task_queue_depth",
                    "Dimensions": [env_dim, {"Name": "TaskType", "Value": clean_type}],
                    "Timestamp": now,
                    "Value": float(count),
                    "Unit": "Count",
                }
            )

        try:
            cw.put_metric_data(
                Namespace=self.namespace,
                MetricData=metric_data,
            )
            logger.info(
                f"[QueueMetricsPublisher] Published CloudWatch metrics: "
                f"depth={metrics['queue_depth']}, oldest_age={metrics['oldest_task_age_seconds']:.1f}s, "
                f"active={metrics['active_tasks']}"
            )
            return True
        except Exception as e:
            # Failure isolation: CloudWatch publication failures must NEVER affect database or task queues
            logger.warning(f"[QueueMetricsPublisher] CloudWatch put_metric_data failure: {e}")
            return False

    def step(self) -> Dict[str, Any]:
        """Performs a single collection and publish iteration."""
        metrics = self.collect_queue_metrics()
        self.publish_metrics_to_cloudwatch(metrics)
        return metrics

    def run_loop(self) -> None:
        """Runs the continuous polling loop."""
        self._running = True
        logger.info(
            f"[QueueMetricsPublisher] Starting independent queue metrics loop "
            f"(interval={self.poll_interval_seconds}s, namespace={self.namespace})..."
        )
        while self._running:
            try:
                self.step()
            except Exception as e:
                logger.error(f"[QueueMetricsPublisher] Unexpected error in step: {e}")
            time.sleep(self.poll_interval_seconds)

    def stop(self) -> None:
        """Stops the loop gracefully."""
        self._running = False
        logger.info("[QueueMetricsPublisher] Stopped metrics publisher.")


def run_standalone_metrics_publisher() -> None:
    """Production entry point for dedicated metrics publisher container."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )
    logger.info("==================================================================")
    logger.info("STARTING STANDALONE TASK QUEUE METRICS PUBLISHER PROCESS")
    logger.info("==================================================================")

    interval = float(os.getenv("METRICS_POLL_INTERVAL_SECONDS", "15.0"))
    publisher = QueueMetricsPublisher(poll_interval_seconds=interval)

    def handle_signal(sig, frame):
        logger.info("[MetricsPublisher] Received termination signal. Shutting down...")
        publisher.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    publisher.run_loop()


if __name__ == "__main__":
    run_standalone_metrics_publisher()
