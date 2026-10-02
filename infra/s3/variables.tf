variable "project_name" {
  description = "Project name"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "account_id" {
  description = "AWS Account ID"
  type        = string
}

variable "kms_master_key_arn" {
  description = "KMS CMK ARN for S3 server-side encryption"
  type        = string
}
