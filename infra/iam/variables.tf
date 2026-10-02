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

variable "app_kms_key_arn" {
  description = "Application KMS CMK ARN"
  type        = string
}

variable "s3_documents_bucket_arn" {
  description = "S3 Documents Bucket ARN"
  type        = string
}

variable "opensearch_domain_arn" {
  description = "OpenSearch Domain ARN"
  type        = string
}
