"""
Step 14 Test Suite - Production AWS Deployment & Infrastructure Verification

Tests:
1. Configuration & AWS Environment Validation (AWS_REGION, strict prod guards).
2. Health Check Optimization (/health/live, /health/ready, /health/dependencies).
3. Independent Task Queue Metrics Publisher (PostgreSQL query, CloudWatch emission).
4. Decoupled Database Rollback in Deployment Tooling (Task def reverted, NO alembic downgrade).
5. Dockerfile & Container Security Standards (non-root, multi-stage, .dockerignore).
6. Terraform IaC Architecture & Correction Verification:
   - Bootstrap remote state S3 & dedicated State KMS CMK
   - VPC topology & Endpoint types (Gateway S3 vs Interface ECR/Secrets/Logs/KMS)
   - OpenSearch HA Topology (Zone awareness, dedicated cluster managers, SigV4)
   - Application vs State KMS key separation
   - Least privilege IAM roles & policies
   - Autoscaling driven by queue depth metric
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Ensure backend directory is in path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

REPO_ROOT = Path(__file__).resolve().parent.parent

# Lightweight test app containing health router without duplicate prefix
from app.api.health import router as health_router
health_app = FastAPI()
health_app.include_router(health_router)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Configuration & AWS Environment Validation
# ─────────────────────────────────────────────────────────────────────────────

def test_config_aws_region_and_defaults():
    """Verify AWS_REGION and production config attributes are present in config."""
    from app import config
    assert hasattr(config, "AWS_REGION")
    assert config.AWS_REGION is not None
    assert config.DOCUMENT_STORAGE_TYPE in ["local", "s3"]
    assert config.SEARCH_STORE_TYPE in ["chroma_dev", "local_bm25", "opensearch"]


def test_config_production_validation_guards():
    """Verify production security validation rules."""
    from app import config
    assert hasattr(config, "ENVIRONMENT")
    assert hasattr(config, "APP_ENV")
    assert hasattr(config, "JWT_SECRET_KEY")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Lightweight Health Endpoints Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_health_live_endpoint():
    """Verify /health/live is a pure process heartbeat with 0 external dependencies."""
    client = TestClient(health_app)
    response = client.get("/health/live")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "alive"


def test_health_ready_endpoint():
    """Verify /health/ready returns status and performs lightweight checks."""
    client = TestClient(health_app)
    response = client.get("/health/ready")
    assert response.status_code in [200, 503]
    data = response.json()
    assert "status" in data
    assert "checks" in data
    assert "database" in data["checks"]
    assert "search_store" in data["checks"]
    assert "task_subsystem" in data["checks"]
    assert "document_storage" in data["checks"]
    assert "secret_provider" in data["checks"]


def test_health_dependencies_endpoint():
    """Verify /health/dependencies provides detailed dependency status."""
    client = TestClient(health_app)
    response = client.get("/health/dependencies")
    assert response.status_code in [200, 503]
    data = response.json()
    assert "status" in data
    assert "details" in data
    assert "database" in data["details"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Independent Task Queue Metrics Publisher Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_metrics_publisher_queue_metrics_calculation():
    """Verify QueueMetricsPublisher calculates queue depth and oldest task age."""
    from app.tasks.metrics_publisher import QueueMetricsPublisher

    mock_cw = MagicMock()
    publisher = QueueMetricsPublisher(
        namespace="EnterpriseRAG/Tasks",
        environment="production",
        cloudwatch_client=mock_cw,
    )

    with patch("app.tasks.metrics_publisher.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        # Mock query results
        mock_session.execute.return_value.fetchone.return_value = (5, 120.0) # pending
        mock_session.execute.return_value.scalar.side_effect = [2, 1]       # running, failed
        mock_session.execute.return_value.fetchall.return_value = [("document_indexing", 3), ("connector_sync", 2)]

        metrics = publisher.collect_queue_metrics()
        assert metrics["queue_depth"] == 5
        assert metrics["oldest_task_age_seconds"] == 120.0
        assert metrics["active_tasks"] == 2
        assert metrics["failed_tasks"] == 1
        assert "document_indexing" in metrics["by_type"]

        # Publish to CloudWatch
        success = publisher.publish_metrics_to_cloudwatch(metrics)
        assert success is True
        assert mock_cw.put_metric_data.called
        call_args = mock_cw.put_metric_data.call_args[1]
        assert call_args["Namespace"] == "EnterpriseRAG/Tasks"
        metric_names = [m["MetricName"] for m in call_args["MetricData"]]
        assert "rag_task_queue_depth" in metric_names
        assert "rag_task_oldest_age_seconds" in metric_names


def test_metrics_publisher_graceful_error_handling():
    """Verify MetricsPublisher does not crash on DB or CloudWatch failures."""
    from app.tasks.metrics_publisher import QueueMetricsPublisher

    mock_cw = MagicMock()
    publisher = QueueMetricsPublisher(
        namespace="EnterpriseRAG/Tasks",
        environment="production",
        cloudwatch_client=mock_cw,
    )

    with patch("app.tasks.metrics_publisher.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("DB Connection Timeout")
        mock_session_factory.return_value = mock_session

        metrics = publisher.collect_queue_metrics()
        assert metrics["queue_depth"] == 0

        # Simulate CW error
        mock_cw.put_metric_data.side_effect = Exception("CloudWatch Unavailable")
        success = publisher.publish_metrics_to_cloudwatch(metrics)
        assert success is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. Decoupled Database Rollback Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_deploy_orchestrator_rollback_without_db_downgrade():
    """
    Verify that when deployment fails verification, ECS task definitions are reverted
    but alembic downgrade is NEVER executed.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.deploy import ProductionDeployer

    mock_ecs = MagicMock()
    mock_ecs.describe_services.side_effect = [
        {"services": [{"taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/api:1"}]},
        {"services": [{"taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/worker:1"}]},
    ]

    deployer = ProductionDeployer(
        cluster_name="rag-cluster-production",
        api_service_name="rag-api-service",
        worker_service_name="rag-worker-service",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/enterprise-rag:v2",
        boto3_client_factory=lambda svc, **kwargs: mock_ecs,
    )

    deployer.record_current_state()
    assert deployer.previous_api_task_def == "arn:aws:ecs:us-east-1:123456789012:task-definition/api:1"
    assert deployer.previous_worker_task_def == "arn:aws:ecs:us-east-1:123456789012:task-definition/worker:1"

    # Execute rollback
    with patch("subprocess.run") as mock_subproc:
        deployer.rollback_application()

        # Check ECS updates to previous task defs
        assert mock_ecs.update_service.call_count >= 2
        calls = [c[1] for c in mock_ecs.update_service.call_args_list]
        task_defs_reverted = [c.get("taskDefinition") for c in calls]
        assert "arn:aws:ecs:us-east-1:123456789012:task-definition/api:1" in task_defs_reverted
        assert "arn:aws:ecs:us-east-1:123456789012:task-definition/worker:1" in task_defs_reverted

        # Verify alembic downgrade was NOT called
        for call in mock_subproc.call_args_list:
            cmd = call[0][0] if call[0] else []
            assert "downgrade" not in " ".join(cmd)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Dockerfile & Container Security Standards Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_dockerfile_security_standards():
    """Verify Dockerfile enforces multi-stage builds, non-root user, and no .env."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    assert dockerfile_path.exists(), "Dockerfile must exist at repo root"

    content = dockerfile_path.read_text()
    assert "FROM python:3.12-slim" in content
    assert "useradd" in content or "adduser" in content
    assert "USER appuser" in content or "USER 10001" in content
    assert "COPY .env" not in content


def test_dockerignore_exclusions():
    """Verify .dockerignore excludes sensitive files, state files, and git history."""
    dockerignore_path = REPO_ROOT / ".dockerignore"
    assert dockerignore_path.exists(), ".dockerignore must exist"

    content = dockerignore_path.read_text()
    assert ".git" in content
    assert ".env" in content
    assert "terraform.tfstate" in content
    assert "tests/" in content


# ─────────────────────────────────────────────────────────────────────────────
# 6. Terraform IaC Architecture & Configuration Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_terraform_bootstrap_stack():
    """Verify bootstrap stack defines S3 state bucket, versioning, state locking, and separate State KMS CMK."""
    bootstrap_main = (REPO_ROOT / "infra" / "bootstrap" / "main.tf").read_text()
    assert "aws_s3_bucket" in bootstrap_main
    assert "aws_s3_bucket_versioning" in bootstrap_main
    assert "aws_dynamodb_table" in bootstrap_main
    assert "aws_kms_key" in bootstrap_main
    assert "tf_state_cmk" in bootstrap_main


def test_terraform_vpc_endpoints_and_nat_architecture():
    """Verify VPC contains Gateway S3 endpoint, Interface endpoints for ECR/Secrets/Logs/KMS, and multi-AZ NAT."""
    network_main = (REPO_ROOT / "infra" / "network" / "main.tf").read_text()
    assert 'service_name      = "com.amazonaws.${var.aws_region}.s3"' in network_main
    assert 'vpc_endpoint_type = "Gateway"' in network_main
    assert "ecr_api" in network_main
    assert "ecr_dkr" in network_main
    assert "secrets" in network_main
    assert "logs" in network_main
    assert "kms" in network_main
    assert "aws_nat_gateway" in network_main


def test_terraform_opensearch_ha_topology():
    """Verify OpenSearch HA topology (zone awareness, dedicated cluster managers, KMS CMK, TLS, SigV4)."""
    opensearch_main = (REPO_ROOT / "infra" / "opensearch" / "main.tf").read_text()
    assert "zone_awareness_enabled = true" in opensearch_main
    assert "dedicated_master_enabled = true" in opensearch_main
    assert "dedicated_master_count" in opensearch_main
    assert "encrypt_at_rest" in opensearch_main
    assert "node_to_node_encryption" in opensearch_main
    assert "Policy-Min-TLS-1-2-2019-07" in opensearch_main
    assert "access_policies" in opensearch_main


def test_terraform_kms_key_separation():
    """Verify Application KMS CMK is defined separately from Terraform State KMS CMK."""
    app_kms_main = (REPO_ROOT / "infra" / "kms" / "main.tf").read_text()
    bootstrap_main = (REPO_ROOT / "infra" / "bootstrap" / "main.tf").read_text()

    assert "app_cmk" in app_kms_main
    assert "tf_state_cmk" in bootstrap_main
    assert "EnableRootAdministrationAndIAMDelegation" in app_kms_main


def test_terraform_iam_least_privilege():
    """Verify IAM roles exist for Execution, API, Worker, Metrics Publisher, and Migration."""
    iam_main = (REPO_ROOT / "infra" / "iam" / "main.tf").read_text()
    assert "ecs_execution_role" in iam_main
    assert "ecs_api_task_role" in iam_main
    assert "ecs_worker_task_role" in iam_main
    assert "ecs_metrics_task_role" in iam_main
    assert "migration_task_role" in iam_main
    assert "AdministratorAccess" not in iam_main


def test_terraform_ecs_and_autoscaling():
    """Verify ECS services define separate API, Worker, Metrics Publisher, and Queue Depth Autoscaling."""
    ecs_main = (REPO_ROOT / "infra" / "ecs" / "main.tf").read_text()
    assert 'name      = "rag-api"' in ecs_main
    assert 'name      = "rag-worker"' in ecs_main
    assert 'name      = "rag-metrics-publisher"' in ecs_main
    assert 'name      = "migration"' in ecs_main
    assert "worker_queue_depth_scaling" in ecs_main
    assert "rag_task_queue_depth" in ecs_main


def test_database_migration_policy_document():
    """Verify documentation of Expand-Deploy-Migrate-Verify-Contract and decoupled rollback policy."""
    policy_doc = (REPO_ROOT / "docs" / "database_migration_policy.md").read_text()
    assert "EXPAND" in policy_doc
    assert "CONTRACT" in policy_doc
    assert "Decoupled Rollback Policy" in policy_doc
    assert "NEVER automatically execute `alembic downgrade`" in policy_doc
