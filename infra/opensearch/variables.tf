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

variable "account_id" {
  description = "AWS Account ID"
  type        = string
}

variable "subnet_ids" {
  description = "List of private database/search subnet IDs"
  type        = list(string)
}

variable "security_group_id" {
  description = "OpenSearch Security Group ID"
  type        = string
}

variable "kms_master_key_arn" {
  description = "KMS CMK ARN for OpenSearch encryption at rest"
  type        = string
}

variable "instance_type" {
  description = "Data node instance type"
  type        = string
}

variable "instance_count" {
  description = "Data node instance count"
  type        = number
}

variable "dedicated_master_count" {
  description = "Dedicated cluster manager node count"
  type        = number
}

variable "dedicated_master_type" {
  description = "Dedicated cluster manager instance type"
  type        = string
}

variable "volume_size_gb" {
  description = "EBS volume size per node in GB"
  type        = number
}

variable "ecs_api_task_role_arn" {
  description = "ECS API Task Role ARN for SigV4 policy"
  type        = string
}

variable "ecs_worker_task_role_arn" {
  description = "ECS Worker Task Role ARN for SigV4 policy"
  type        = string
}
