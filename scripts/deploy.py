#!/usr/bin/env python3
"""
Enterprise RAG - Production AWS Deployment Orchestrator

This script orchestrates the production deployment lifecycle:
1. Pre-deployment schema migration (runs Alembic upgrade head as a dedicated task).
2. Rolling update of ECS API and Worker services.
3. Health verification using lightweight /health/ready probe.
4. Automatic application rollback upon verification failure (reverts task definition),
   CRITICAL: Database downgrade is NEVER automated (Decoupled DB Rollback Policy).
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("deploy-orchestrator")


class DeploymentError(Exception):
    """Raised when any deployment stage fails."""


class ProductionDeployer:
    """Manages multi-stage AWS production deployment and decoupled rollback."""

    def __init__(
        self,
        cluster_name: str,
        api_service_name: str,
        worker_service_name: str,
        image_uri: str,
        aws_region: str = "us-east-1",
        boto3_client_factory: Optional[Any] = None,
    ):
        self.cluster_name = cluster_name
        self.api_service_name = api_service_name
        self.worker_service_name = worker_service_name
        self.image_uri = image_uri
        self.aws_region = aws_region
        self._boto3_factory = boto3_client_factory

        self.previous_api_task_def: Optional[str] = None
        self.previous_worker_task_def: Optional[str] = None

    def _get_client(self, service: str):
        if self._boto3_factory:
            return self._boto3_factory(service, region_name=self.aws_region)
        import boto3
        return boto3.client(service, region_name=self.aws_region)

    def record_current_state(self) -> None:
        """Capture currently active task definitions for safe application rollback."""
        logger.info("Capturing current active task definitions for rollback baseline...")
        ecs = self._get_client("ecs")
        try:
            api_resp = ecs.describe_services(
                cluster=self.cluster_name,
                services=[self.api_service_name],
            )
            if api_resp.get("services"):
                self.previous_api_task_def = api_resp["services"][0].get("taskDefinition")

            worker_resp = ecs.describe_services(
                cluster=self.cluster_name,
                services=[self.worker_service_name],
            )
            if worker_resp.get("services"):
                self.previous_worker_task_def = worker_resp["services"][0].get("taskDefinition")

            logger.info(
                "Baseline recorded - API TaskDef: %s, Worker TaskDef: %s",
                self.previous_api_task_def,
                self.previous_worker_task_def,
            )
        except Exception as exc:
            logger.warning("Could not record baseline task definitions: %s", exc)

    def run_pre_deployment_migrations(self, migration_task_def: str, subnets: list, security_groups: list) -> bool:
        """
        Execute schema migrations as a dedicated one-off task before rolling out new code.
        Forward-compatible migrations (Expand phase) ensure zero downtime.
        """
        logger.info("Stage 1: Running pre-deployment database migrations...")
        ecs = self._get_client("ecs")
        try:
            response = ecs.run_task(
                cluster=self.cluster_name,
                taskDefinition=migration_task_def,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": security_groups,
                        "assignPublicIp": "DISABLED",
                    }
                },
            )
            tasks = response.get("tasks", [])
            if not tasks:
                raise DeploymentError("Failed to launch database migration task.")

            task_arn = tasks[0]["taskArn"]
            logger.info("Migration task launched: %s. Awaiting completion...", task_arn)

            # In mock/test environments or live waiter:
            waiter = ecs.get_waiter("tasks_stopped") if hasattr(ecs, "get_waiter") else None
            if waiter:
                waiter.wait(cluster=self.cluster_name, tasks=[task_arn])

            desc = ecs.describe_tasks(cluster=self.cluster_name, tasks=[task_arn])
            task_desc = desc["tasks"][0]
            containers = task_desc.get("containers", [])
            exit_code = containers[0].get("exitCode", 0) if containers else 0

            if exit_code != 0:
                raise DeploymentError(f"Database migration task failed with exit code {exit_code}")

            logger.info("Database migrations completed successfully.")
            return True
        except Exception as exc:
            logger.error("Pre-deployment migration failed: %s", exc)
            raise DeploymentError(f"Migration failure: {exc}") from exc

    def update_services(self, new_api_task_def: str, new_worker_task_def: str) -> None:
        """Perform rolling update on ECS API and Worker services."""
        logger.info("Stage 2: Initiating rolling update of ECS services...")
        ecs = self._get_client("ecs")

        # Update API service
        logger.info("Updating API service '%s' to task definition: %s", self.api_service_name, new_api_task_def)
        ecs.update_service(
            cluster=self.cluster_name,
            service=self.api_service_name,
            taskDefinition=new_api_task_def,
            forceNewDeployment=True,
        )

        # Update Worker service
        logger.info("Updating Worker service '%s' to task definition: %s", self.worker_service_name, new_worker_task_def)
        ecs.update_service(
            cluster=self.cluster_name,
            service=self.worker_service_name,
            taskDefinition=new_worker_task_def,
            forceNewDeployment=True,
        )

    def verify_health(self, ready_url: str, max_retries: int = 12, interval_sec: int = 10) -> bool:
        """
        Verify that new deployment is serving traffic via lightweight /health/ready probe.
        """
        logger.info("Stage 3: Verifying deployment health via %s...", ready_url)
        import urllib.request
        import urllib.error

        for attempt in range(1, max_retries + 1):
            try:
                req = urllib.request.Request(ready_url, headers={"User-Agent": "DeployHealthCheck/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode())
                        if body.get("status") == "ready":
                            logger.info("Health check PASSED on attempt %d/%d.", attempt, max_retries)
                            return True
            except Exception as exc:
                logger.debug("Health probe attempt %d/%d failed: %s", attempt, max_retries, exc)

            time.sleep(interval_sec)

        logger.error("Health check timed out after %d attempts.", max_retries)
        return False

    def rollback_application(self) -> None:
        """
        Revert ECS services to previous known-good task definitions.
        CRITICAL: Never automatically downgrade database migrations.
        """
        logger.critical("Initiating APPLICATION ROLLBACK to previous task definitions...")
        ecs = self._get_client("ecs")

        if self.previous_api_task_def:
            logger.info("Reverting API service to: %s", self.previous_api_task_def)
            try:
                ecs.update_service(
                    cluster=self.cluster_name,
                    service=self.api_service_name,
                    taskDefinition=self.previous_api_task_def,
                )
            except Exception as e:
                logger.error("Failed to revert API service: %s", e)

        if self.previous_worker_task_def:
            logger.info("Reverting Worker service to: %s", self.previous_worker_task_def)
            try:
                ecs.update_service(
                    cluster=self.cluster_name,
                    service=self.worker_service_name,
                    taskDefinition=self.previous_worker_task_def,
                )
            except Exception as e:
                logger.error("Failed to revert Worker service: %s", e)

        # Operational decoupling notice:
        logger.warning("=" * 70)
        logger.warning("DATABASE ROLLBACK POLICY NOTICE:")
        logger.warning("Application workloads have been reverted to previous versions.")
        logger.warning("Automated database downgrades are NOT performed to prevent data loss.")
        logger.warning("If database schema changes need inspection or manual rollback:")
        logger.warning("  1. Review Alembic revision history: alembic history")
        logger.warning("  2. Verify data integrity and backwards compatibility.")
        logger.warning("  3. Run manual migration rollback only if deemed safe: alembic downgrade <rev>")
        logger.warning("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Production AWS Deployment Orchestrator")
    parser.add_argument("--cluster", required=True, help="ECS Cluster name")
    parser.add_argument("--api-service", required=True, help="API ECS Service name")
    parser.add_argument("--worker-service", required=True, help="Worker ECS Service name")
    parser.add_argument("--image-uri", required=True, help="Container Image URI")
    parser.add_argument("--new-api-task-def", required=True, help="New API Task Definition ARN")
    parser.add_argument("--new-worker-task-def", required=True, help="New Worker Task Definition ARN")
    parser.add_argument("--migration-task-def", required=False, help="Migration Task Definition ARN")
    parser.add_argument("--health-url", required=True, help="URL for /health/ready check")
    parser.add_argument("--region", default="us-east-1", help="AWS Region")
    parser.add_argument("--subnets", nargs="*", default=[], help="Subnet IDs for migration task")
    parser.add_argument("--security-groups", nargs="*", default=[], help="SG IDs for migration task")
    args = parser.parse_args()

    deployer = ProductionDeployer(
        cluster_name=args.cluster,
        api_service_name=args.api_service,
        worker_service_name=args.worker_service,
        image_uri=args.image_uri,
        aws_region=args.region,
    )

    try:
        deployer.record_current_state()

        if args.migration_task_def and args.subnets and args.security_groups:
            deployer.run_pre_deployment_migrations(
                migration_task_def=args.migration_task_def,
                subnets=args.subnets,
                security_groups=args.security_groups,
            )

        deployer.update_services(
            new_api_task_def=args.new_api_task_def,
            new_worker_task_def=args.new_worker_task_def,
        )

        healthy = deployer.verify_health(args.health_url)
        if not healthy:
            logger.error("Deployment health check failed! Triggering application rollback.")
            deployer.rollback_application()
            sys.exit(1)

        logger.info("Deployment succeeded and verified healthy.")
    except Exception as exc:
        logger.error("Deployment failed: %s", exc)
        deployer.rollback_application()
        sys.exit(1)


if __name__ == "__main__":
    main()
