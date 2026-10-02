variable "project_name" {
  description = "Project name"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "kms_master_key_arn" {
  description = "KMS CMK ARN for CloudWatch log encryption"
  type        = string
}

variable "alb_arn_suffix" {
  description = "ALB ARN suffix for metric dimensions"
  type        = string
}

variable "db_instance_id" {
  description = "RDS DB instance identifier for metric dimensions"
  type        = string
}
