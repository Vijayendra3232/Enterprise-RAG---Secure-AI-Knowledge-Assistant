output "state_bucket_name" {
  description = "Name of the S3 Remote State Bucket"
  value       = aws_s3_bucket.tf_state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the S3 Remote State Bucket"
  value       = aws_s3_bucket.tf_state.arn
}

output "state_kms_key_arn" {
  description = "ARN of the dedicated Terraform State KMS CMK"
  value       = aws_kms_key.tf_state_cmk.arn
}

output "backend_config_snippet" {
  description = "Snippet to paste into infra/backend.tf"
  value = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.tf_state.bucket}"
        key            = "${var.environment}/terraform.tfstate"
        region         = "${var.aws_region}"
        encrypt        = true
        kms_key_id     = "${aws_kms_key.tf_state_cmk.arn}"
      }
    }
  EOT
}
