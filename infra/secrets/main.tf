# Production AWS Secrets Manager Configuration
resource "aws_secretsmanager_secret" "database" {
  name                    = "${var.project_name}/${var.environment}/database"
  description             = "Database credentials for PostgreSQL"
  kms_key_id              = var.kms_master_key_arn
  recovery_window_in_days = 7

  tags = {
    Name = "${var.project_name}-secret-database-${var.environment}"
  }
}

resource "aws_secretsmanager_secret" "auth" {
  name                    = "${var.project_name}/${var.environment}/auth"
  description             = "Authentication and Telemetry secrets (JWT, HMAC, Monitoring Key)"
  kms_key_id              = var.kms_master_key_arn
  recovery_window_in_days = 7

  tags = {
    Name = "${var.project_name}-secret-auth-${var.environment}"
  }
}

resource "aws_secretsmanager_secret" "llm" {
  name                    = "${var.project_name}/${var.environment}/llm"
  description             = "LLM provider API keys"
  kms_key_id              = var.kms_master_key_arn
  recovery_window_in_days = 7

  tags = {
    Name = "${var.project_name}-secret-llm-${var.environment}"
  }
}

resource "aws_secretsmanager_secret" "connectors" {
  name                    = "${var.project_name}/${var.environment}/connectors"
  description             = "Cloud connector credentials for Google Drive and Microsoft Graph"
  kms_key_id              = var.kms_master_key_arn
  recovery_window_in_days = 7

  tags = {
    Name = "${var.project_name}-secret-connectors-${var.environment}"
  }
}
