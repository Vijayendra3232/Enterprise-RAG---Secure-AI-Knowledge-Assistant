# Staging Environment Terraform Variables
aws_region          = "us-east-1"
environment         = "staging"
project_name        = "enterprise-rag"
domain_name         = "api.staging.rag.enterprise.internal"
vpc_cidr            = "10.100.0.0/16"
availability_zones  = ["us-east-1a", "us-east-1b", "us-east-1c"]

container_image     = "157975549966.dkr.ecr.us-east-1.amazonaws.com/enterprise-rag:staging-v1.0.0"

opensearch_instance_type          = "r6g.large.search"
opensearch_instance_count         = 3
opensearch_dedicated_master_count = 3
opensearch_dedicated_master_type  = "m6g.large.search"
opensearch_volume_size_gb         = 100

db_instance_class       = "db.r6g.large"
db_allocated_storage_gb = 100

api_min_tasks    = 2
api_max_tasks    = 6
worker_min_tasks = 2
worker_max_tasks = 6
