#!/usr/bin/env python3
"""
Enterprise RAG - Manual Application Rollback Tool

Reverts ECS services to specified task definitions or prior revisions.
Strictly decoupled from database downgrades.
"""

import argparse
import logging
import sys
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("rollback-tool")


def rollback_ecs_service(cluster: str, service: str, target_task_def: str, region: str = "us-east-1"):
    """Update an ECS service to a specific prior task definition."""
    import boto3
    ecs = boto3.client("ecs", region_name=region)
    logger.info("Reverting service '%s' in cluster '%s' to '%s'...", service, cluster, target_task_def)
    ecs.update_service(
        cluster=cluster,
        service=service,
        taskDefinition=target_task_def,
        forceNewDeployment=True,
    )
    logger.info("Service '%s' rollback initiated.", service)


def main():
    parser = argparse.ArgumentParser(description="Rollback Enterprise RAG ECS Services")
    parser.add_argument("--cluster", required=True, help="ECS Cluster Name")
    parser.add_argument("--api-service", required=True, help="API Service Name")
    parser.add_argument("--api-task-def", required=True, help="Target API Task Definition ARN")
    parser.add_argument("--worker-service", required=False, help="Worker Service Name")
    parser.add_argument("--worker-task-def", required=False, help="Target Worker Task Definition ARN")
    parser.add_argument("--region", default="us-east-1", help="AWS Region")
    args = parser.parse_args()

    try:
        rollback_ecs_service(args.cluster, args.api_service, args.api_task_def, args.region)
        if args.worker_service and args.worker_task_def:
            rollback_ecs_service(args.cluster, args.worker_service, args.worker_task_def, args.region)
        logger.info("Application rollback completed successfully.")
    except Exception as exc:
        logger.error("Rollback failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
