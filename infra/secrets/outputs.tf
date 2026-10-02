output "secrets_prefix" {
  description = "Base secret prefix in Secrets Manager"
  value       = "${var.project_name}/${var.environment}"
}

output "database_secret_arn" {
  description = "Database credentials secret ARN"
  value       = aws_secretsmanager_secret.database.arn
}

output "auth_secret_arn" {
  description = "Auth and telemetry secret ARN"
  value       = aws_secretsmanager_secret.auth.arn
}

output "llm_secret_arn" {
  description = "LLM API keys secret ARN"
  value       = aws_secretsmanager_secret.llm.arn
}

output "connectors_secret_arn" {
  description = "Connectors secret ARN"
  value       = aws_secretsmanager_secret.connectors.arn
}
