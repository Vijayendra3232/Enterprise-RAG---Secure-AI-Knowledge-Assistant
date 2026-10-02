variable "project_name" {
  description = "Project name"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "kms_master_key_arn" {
  description = "KMS CMK ARN for Secrets Manager encryption"
  type        = string
}
