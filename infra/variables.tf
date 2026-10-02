variable "aws_region" {
  description = "AWS Region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string
  default     = "production"
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "Environment must be one of: dev, staging, production."
  }
}

variable "project_name" {
  description = "Project name identifier"
  type        = string
  default     = "enterprise-rag"
}

variable "domain_name" {
  description = "Fully qualified domain name for API (e.g. api.example.com)"
  type        = string
  default     = "api.rag.enterprise.internal"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "List of Availability Zones (minimum 2 for production HA)"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "Production deployment requires at least 2 Availability Zones."
  }
}

variable "container_image" {
  description = "Immutable ECR container image URI with digest or tag"
  type        = string
  default     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/enterprise-rag:v1.0.0"
}

# --- OpenSearch Topology Configuration ---
variable "opensearch_instance_type" {
  description = "OpenSearch data node instance type"
  type        = string
  default     = "r6g.large.search"
}

variable "opensearch_instance_count" {
  description = "Number of OpenSearch data nodes (must be even multiple of AZ count for HA)"
  type        = number
  default     = 2
  validation {
    condition     = var.opensearch_instance_count >= 2
    error_message = "Production OpenSearch HA requires at least 2 data nodes."
  }
}

variable "opensearch_dedicated_master_count" {
  description = "Number of dedicated cluster manager nodes (must be 3 for quorum in HA)"
  type        = number
  default     = 3
}

variable "opensearch_dedicated_master_type" {
  description = "Instance type for dedicated cluster managers"
  type        = string
  default     = "m6g.large.search"
}

variable "opensearch_volume_size_gb" {
  description = "EBS volume size per OpenSearch node in GB"
  type        = number
  default     = 100
}

# --- Database Configuration ---
variable "db_instance_class" {
  description = "RDS PostgreSQL instance class"
  type        = string
  default     = "db.r6g.xlarge"
}

variable "db_allocated_storage_gb" {
  description = "Allocated storage for PostgreSQL in GB"
  type        = number
  default     = 100
}

# --- ECS Scaling Configuration ---
variable "api_min_tasks" {
  description = "Minimum running tasks for rag-api"
  type        = number
  default     = 2
}

variable "api_max_tasks" {
  description = "Maximum running tasks for rag-api"
  type        = number
  default     = 10
}

variable "worker_min_tasks" {
  description = "Minimum running tasks for rag-worker"
  type        = number
  default     = 2
}

variable "worker_max_tasks" {
  description = "Maximum running tasks for rag-worker"
  type        = number
  default     = 10
}
