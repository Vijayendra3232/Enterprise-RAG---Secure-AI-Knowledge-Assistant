# Step 14 Walkthrough — Production AWS Deployment

## 1. Executive Summary

Step 14 establishes a **production-grade, secure, multi-AZ AWS infrastructure** for the Enterprise RAG platform using **Terraform (Infrastructure as Code)**, **Amazon ECS Fargate**, **Amazon RDS PostgreSQL Multi-AZ**, **Amazon OpenSearch Service HA**, **Amazon S3 with SSE-KMS CMK**, **AWS Secrets Manager**, and **Application Load Balancers (ALB)**.

All architectural invariants from Steps 1–13 are preserved:
- **PostgreSQL Single Source of Truth**: PostgreSQL remains authoritative for all tenants, users, permissions, document metadata, task states, and audit trails. OpenSearch is strictly a derived search index.
- **Decoupled Database Rollback**: Automated application rollback reverts ECS task definitions and search aliases, but **never automatically downgrades database schema**. Database rollback is an explicit operational procedure.
- **Independent Autoscaling**: `rag-metrics-publisher` runs as an independent daemon publishing CloudWatch queue metrics to drive worker autoscaling without circular dependency on worker health.
- **Strict Network Isolation**: All workloads, databases, and search clusters reside in private subnets with least-privilege VPC endpoints (S3 Gateway, Interface Endpoints for ECR/Secrets/Logs/KMS) and multi-AZ NAT Gateways.
- **Least-Privilege IAM Roles & Key Separation**: Separate execution, API, worker, metrics, and migration roles with no `AdministratorAccess`. Dedicated Application KMS CMK is strictly isolated from the Terraform State KMS CMK.

---

## 2. Target Architecture Diagram

```
                                  Internet
                                     │
                                     ▼
                      ┌─────────────────────────────┐
                      │    Route 53 DNS Record      │
                      └──────────────┬──────────────┘
                                     │
                                     ▼
                      ┌─────────────────────────────┐
                      │  Application Load Balancer  │
                      │  (Public Subnets / TLS 1.3) │
                      └──────────────┬──────────────┘
                                     │
                ┌────────────────────┴────────────────────┐
                │                                         │
                ▼                                         ▼
┌───────────────────────────────┐         ┌───────────────────────────────┐
│       ECS API Service         │         │      ECS Worker Service       │
│  (Private App Subnets / AZs)  │         │  (Private App Subnets / AZs)  │
└───────┬───────────────┬───────┘         └───────┬───────────────┬───────┘
        │               │                         │               │
        │               │   ┌─────────────────────┤               │
        │               │   │                     │               │
        ▼               ▼   ▼                     ▼               ▼
┌──────────────┐ ┌──────────────┐         ┌──────────────┐ ┌──────────────┐
│  Amazon S3   │ │Amazon RDS PG │         │OpenSearch HA │ │Independent   │
│  (SSE-KMS)   │ │  (Multi-AZ)  │         │(Multi-AZ +   │ │Metrics Pub.  │
│  S3 Endpoint │ │  Encrypted   │         │ 3 Masters)   │ │(CloudWatch)  │
└──────────────┘ └──────────────┘         └──────────────┘ └──────────────┘
```

---

## 3. Production Hardening & Architectural Corrections

| # | Architecture Invariant | Implementation Details |
|---|---|---|
| **1** | **Decoupled Database Rollback** | `scripts/deploy.py` reverts ECS task definitions upon failed health check, but **never automatically executes `alembic downgrade`**. Policy documented in [`docs/database_migration_policy.md`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/docs/database_migration_policy.md). |
| **2** | **Terraform Bootstrap Stack** | Dedicated [`infra/bootstrap/`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/infra/bootstrap/) provisions remote S3 bucket, DynamoDB lock table, and State KMS CMK before root `terraform init`. |
| **3** | **OpenSearch HA Topology** | [`infra/opensearch/main.tf`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/infra/opensearch/main.tf) enforces multi-AZ zone awareness, 2–4 data nodes, 3 dedicated cluster-manager nodes, replicas $\ge 1$, KMS CMK, TLS 1.2, and SigV4 IAM access policy. |
| **4** | **KMS Key Separation** | Application CMK ([`infra/kms/`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/infra/kms/)) for documents and secrets is completely separated from Terraform State CMK ([`infra/bootstrap/`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/infra/bootstrap/)). ECS runtime roles are explicitly denied access to state keys. |
| **5** | **VPC Endpoints & NATs** | S3 Gateway Endpoint (free & HA), Interface Endpoints for ECR API, ECR DKR, Secrets Manager, CloudWatch Logs, and KMS. Outbound to external SaaS (Google Drive, MS Graph, Groq) via multi-AZ NAT Gateways. |
| **6** | **Independent Metrics Publisher** | [`backend/app/tasks/metrics_publisher.py`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/backend/app/tasks/metrics_publisher.py) queries PostgreSQL task queue and publishes `rag_task_queue_depth` and `rag_task_oldest_age_seconds` to CloudWatch for TargetTracking autoscaling. |
| **7** | **Least-Privilege IAM Roles** | Execution Role, API Task Role, Worker Task Role, Metrics Task Role, and Migration Task Role defined with fine-grained Resource and Condition blocks. Zero `AdministratorAccess`. |
| **8** | **Lightweight Health Checks** | [`/health/live`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/backend/app/api/health.py) (process heartbeat, 0 external calls), [`/health/ready`](file:///c:/Users/gorre/OneDrive/Documents/enterprise-rag/backend/app/api/health.py) (`SELECT 1`, OpenSearch ping, S3 config check; 0 heavy RAG, 0 KMS crypto, 0 LLM calls). |

---

## 4. Verification & Test Results

### Step 14 Test Suite (`tests/test_step14_aws_deployment.py`)
- **17/17 tests passed (100%)**
- Verified:
  1. `test_config_aws_region_and_defaults`
  2. `test_config_production_validation_guards`
  3. `test_health_live_endpoint`
  4. `test_health_ready_endpoint`
  5. `test_health_dependencies_endpoint`
  6. `test_metrics_publisher_queue_metrics_calculation`
  7. `test_metrics_publisher_graceful_error_handling`
  8. `test_deploy_orchestrator_rollback_without_db_downgrade`
  9. `test_dockerfile_security_standards`
  10. `test_dockerignore_exclusions`
  11. `test_terraform_bootstrap_stack`
  12. `test_terraform_vpc_endpoints_and_nat_architecture`
  13. `test_terraform_opensearch_ha_topology`
  14. `test_terraform_kms_key_separation`
  15. `test_terraform_iam_least_privilege`
  16. `test_terraform_ecs_and_autoscaling`
  17. `test_database_migration_policy_document`

### Full Regression Suite
```text
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.1.1, pluggy-1.6.0
collected 322 items

316 passed, 6 skipped (live cloud gated), 0 failures in 133.93s
```

---

## 5. Capacity & Scientific Benchmarking Notice

> [!IMPORTANT]
> Actual system throughput, RPS, concurrency limits, and worker autoscaling triggers defined in Step 14 are baseline operational configurations. Rigorous scientific validation, load tests, stress tests, and capacity verification will be executed in **Step 15 (Scale, Concurrency & High-Availability Validation)**.
