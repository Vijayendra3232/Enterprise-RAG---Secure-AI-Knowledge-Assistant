# Customer-Managed Key (CMK) for Application Data & Secrets
resource "aws_kms_key" "app_cmk" {
  description             = "Application Customer-Managed Key for ${var.project_name} (${var.environment})"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAdministrationAndIAMDelegation"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowAWSManagedServicesAttachment"
        Effect = "Allow"
        Principal = {
          Service = [
            "s3.amazonaws.com",
            "secretsmanager.amazonaws.com",
            "rds.amazonaws.com",
            "es.amazonaws.com",
            "logs.amazonaws.com"
          ]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:CreateGrant",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-app-cmk-${var.environment}"
  }
}

resource "aws_kms_alias" "app_cmk_alias" {
  name          = "alias/${var.project_name}-app-cmk-${var.environment}"
  target_key_id = aws_kms_key.app_cmk.key_id
}
