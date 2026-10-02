variable "project_name" {
  description = "Project name"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "aws_region" {
  description = "AWS Region"
  type        = string
}

variable "container_image" {
  description = "Immutable ECR container image URI"
  type        = string
}

variable "private_app_subnet_ids" {
  description = "Private application subnet IDs"
  type        = list(string)
}

variable "api_security_group_id" {
  description = "ECS API Security Group ID"
  type        = string
}

variable "worker_security_group_id" {
  description = "ECS Worker Security Group ID"
  type        = string
}

variable "metrics_security_group_id" {
  description = "ECS Metrics Publisher Security Group ID"
  type        = string
}

variable "ecs_execution_role_arn" {
  description = "ECS Task Execution Role ARN"
  type        = string
}

variable "ecs_api_task_role_arn" {
  description = "ECS API Task Role ARN"
  type        = string
}

variable "ecs_worker_task_role_arn" {
  description = "ECS Worker Task Role ARN"
  type        = string
}

variable "ecs_metrics_task_role_arn" {
  description = "ECS Metrics Publisher Task Role ARN"
  type        = string
}

variable "migration_task_role_arn" {
  description = "Database Migration Task Role ARN"
  type        = string
}

variable "alb_target_group_arn" {
  description = "ALB Target Group ARN"
  type        = string
}

variable "alb_listener_arn" {
  description = "ALB HTTPS Listener ARN"
  type        = string
}

variable "s3_documents_bucket_name" {
  description = "S3 documents bucket name"
  type        = string
}

variable "opensearch_endpoint" {
  description = "Amazon OpenSearch domain endpoint"
  type        = string
}

variable "secrets_prefix" {
  description = "AWS Secrets Manager prefix"
  type        = string
}

variable "api_min_tasks" {
  description = "Minimum API task count"
  type        = number
  default     = 2
}

variable "api_max_tasks" {
  description = "Maximum API task count"
  type        = number
  default     = 10
}

variable "worker_min_tasks" {
  description = "Minimum Worker task count"
  type        = number
  default     = 2
}

variable "worker_max_tasks" {
  description = "Maximum Worker task count"
  type        = number
  default     = 10
}
