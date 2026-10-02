# Root Terraform Module for Enterprise RAG AWS Deployment

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

# 1. Network Subsystem (Multi-AZ VPC, Subnets, NAT Gateways, Endpoints)
module "network" {
  source             = "./network"
  project_name       = var.project_name
  environment        = var.environment
  aws_region         = var.aws_region
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
}

# 2. Security Groups Subsystem (Least-Privilege Ingress/Egress Isolation)
module "security_groups" {
  source       = "./security_groups"
  project_name = var.project_name
  environment  = var.environment
  vpc_id       = module.network.vpc_id
}

# 3. KMS Subsystem (Dedicated Application CMK with Root Delegation)
module "kms" {
  source       = "./kms"
  project_name = var.project_name
  environment  = var.environment
  account_id   = data.aws_caller_identity.current.account_id
}

# 4. S3 Subsystem (Document Storage with SSE-KMS, Versioning, TLS-Enforced)
module "s3" {
  source             = "./s3"
  project_name       = var.project_name
  environment        = var.environment
  account_id         = data.aws_caller_identity.current.account_id
  kms_master_key_arn = module.kms.app_cmk_arn
}

# 5. Secrets Manager Subsystem (Encrypted Runtime Secrets)
module "secrets" {
  source             = "./secrets"
  project_name       = var.project_name
  environment        = var.environment
  kms_master_key_arn = module.kms.app_cmk_arn
}

# 6. RDS Subsystem (PostgreSQL Multi-AZ, Storage Encrypted, SSL Enforced)
module "rds" {
  source                  = "./rds"
  project_name            = var.project_name
  environment             = var.environment
  subnet_ids              = module.network.private_db_subnet_ids
  security_group_id       = module.security_groups.rds_security_group_id
  kms_master_key_arn      = module.kms.app_cmk_arn
  db_instance_class       = var.db_instance_class
  db_allocated_storage_gb = var.db_allocated_storage_gb
}

# 7. IAM Subsystem (Tightened Least-Privilege Runtime & Execution Roles)
module "iam" {
  source                  = "./iam"
  project_name            = var.project_name
  environment             = var.environment
  aws_region              = var.aws_region
  account_id              = data.aws_caller_identity.current.account_id
  app_kms_key_arn         = module.kms.app_cmk_arn
  s3_documents_bucket_arn = module.s3.bucket_arn
  opensearch_domain_arn   = module.opensearch.domain_arn
}

# 8. OpenSearch Subsystem (High Availability Multi-AZ, Dedicated Masters, TLS)
module "opensearch" {
  source                 = "./opensearch"
  project_name           = var.project_name
  environment            = var.environment
  aws_region             = var.aws_region
  account_id             = data.aws_caller_identity.current.account_id
  subnet_ids             = module.network.private_db_subnet_ids
  security_group_id      = module.security_groups.opensearch_security_group_id
  kms_master_key_arn     = module.kms.app_cmk_arn
  instance_type          = var.opensearch_instance_type
  instance_count         = var.opensearch_instance_count
  dedicated_master_count = var.opensearch_dedicated_master_count
  dedicated_master_type  = var.opensearch_dedicated_master_type
  volume_size_gb         = var.opensearch_volume_size_gb

  ecs_api_task_role_arn    = module.iam.ecs_api_task_role_arn
  ecs_worker_task_role_arn = module.iam.ecs_worker_task_role_arn
}

# 9. ALB Subsystem (Application Load Balancer, Target Group with /health/ready probe)
module "alb" {
  source            = "./alb"
  project_name      = var.project_name
  environment       = var.environment
  vpc_id            = module.network.vpc_id
  subnet_ids        = module.network.public_subnet_ids
  security_group_id = module.security_groups.alb_security_group_id
  certificate_arn   = "" # Set in terraform.tfvars for production HTTPS
}

# 10. ECS Subsystem (API Service, Worker Service, Metrics Publisher, Migration Task)
module "ecs" {
  source                  = "./ecs"
  project_name            = var.project_name
  environment             = var.environment
  aws_region              = var.aws_region
  container_image         = var.container_image
  private_app_subnet_ids  = module.network.private_app_subnet_ids
  api_security_group_id   = module.security_groups.ecs_api_security_group_id
  worker_security_group_id = module.security_groups.ecs_worker_security_group_id
  metrics_security_group_id = module.security_groups.ecs_metrics_security_group_id

  ecs_execution_role_arn    = module.iam.ecs_execution_role_arn
  ecs_api_task_role_arn      = module.iam.ecs_api_task_role_arn
  ecs_worker_task_role_arn   = module.iam.ecs_worker_task_role_arn
  ecs_metrics_task_role_arn  = module.iam.ecs_metrics_task_role_arn
  migration_task_role_arn    = module.iam.migration_task_role_arn

  alb_target_group_arn     = module.alb.target_group_arn
  alb_listener_arn         = module.alb.https_listener_arn
  s3_documents_bucket_name = module.s3.bucket_name
  opensearch_endpoint      = module.opensearch.endpoint
  secrets_prefix           = module.secrets.secrets_prefix

  api_min_tasks            = var.api_min_tasks
  api_max_tasks            = var.api_max_tasks
  worker_min_tasks         = var.worker_min_tasks
  worker_max_tasks         = var.worker_max_tasks
}

# 11. Monitoring Subsystem (CloudWatch Logs & Metric Alarms)
module "monitoring" {
  source             = "./monitoring"
  project_name       = var.project_name
  environment        = var.environment
  kms_master_key_arn = module.kms.app_cmk_arn
  alb_arn_suffix     = module.alb.alb_arn_suffix
  db_instance_id     = module.rds.db_instance_id
}
