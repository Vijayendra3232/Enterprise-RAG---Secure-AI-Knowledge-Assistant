variable "project_name" {
  description = "Project name"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "subnet_ids" {
  description = "List of private database subnet IDs"
  type        = list(string)
}

variable "security_group_id" {
  description = "RDS Security Group ID"
  type        = string
}

variable "kms_master_key_arn" {
  description = "KMS CMK ARN for storage and master password encryption"
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
}

variable "db_allocated_storage_gb" {
  description = "Allocated storage in GB"
  type        = number
}
