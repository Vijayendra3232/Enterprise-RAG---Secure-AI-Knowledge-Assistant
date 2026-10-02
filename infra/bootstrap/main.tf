terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "TerraformBootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

# 1. Dedicated Customer-Managed KMS Key for Terraform Remote State
resource "aws_kms_key" "tf_state_cmk" {
  description             = "Dedicated CMK for ${var.project_name} Terraform Remote State (${var.environment})"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAndAdminManagement"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "DenyRuntimeRoleAccess"
        Effect = "Deny"
        Principal = {
          AWS = "*"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
        Condition = {
          StringLike = {
            "aws:PrincipalArn": [
              "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*ecs-api-task*",
              "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/*ecs-worker-task*"
            ]
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "tf_state_cmk_alias" {
  name          = "alias/${var.project_name}-tf-state-${var.environment}"
  target_key_id = aws_kms_key.tf_state_cmk.key_id
}

# 2. S3 Remote State Bucket
resource "aws_s3_bucket" "tf_state" {
  bucket        = "${var.project_name}-tf-state-${var.environment}-${data.aws_caller_identity.current.account_id}"
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

# Enforce Bucket Owner Ownership (ACLs Disabled)
resource "aws_s3_bucket_ownership_controls" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Enforce Block Public Access
resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable S3 Bucket Versioning for State History and Rollback
resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Enable Server-Side Encryption with Dedicated State KMS CMK
resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.tf_state_cmk.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# Restrict Bucket Policy: Enforce HTTPS/TLS and Encryption
resource "aws_s3_bucket_policy" "tf_state" {
  bucket     = aws_s3_bucket.tf_state.id
  depends_on = [aws_s3_bucket_public_access_block.tf_state]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnforceTLSRequestsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.tf_state.arn,
          "${aws_s3_bucket.tf_state.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}

# 3. DynamoDB Table for Terraform State Locking
resource "aws_dynamodb_table" "tf_locks" {
  name         = "${var.project_name}-tf-locks-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.tf_state_cmk.arn
  }

  tags = {
    Name = "${var.project_name}-tf-locks-${var.environment}"
  }
}
