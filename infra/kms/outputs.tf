output "app_cmk_arn" {
  description = "Application KMS CMK ARN"
  value       = aws_kms_key.app_cmk.arn
}

output "app_cmk_id" {
  description = "Application KMS CMK ID"
  value       = aws_kms_key.app_cmk.key_id
}

output "app_cmk_alias_arn" {
  description = "Application KMS CMK Alias ARN"
  value       = aws_kms_alias.app_cmk_alias.arn
}
