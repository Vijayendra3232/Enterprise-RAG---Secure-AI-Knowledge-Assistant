# Tightened Least-Privilege IAM Architecture (Correction 4 & Final Correction 4)

# 1. ECS Task Execution Role (Container Startup & Image Pull)
resource "aws_iam_role" "ecs_execution_role" {
  name = "${var.project_name}-ecs-execution-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "ecs_execution_policy" {
  name = "${var.project_name}-ecs-execution-policy-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ECRImagePull"
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogsDelivery"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/ecs/${var.project_name}-*:*"
      },
      {
        Sid    = "SecretsManagerTaskInjection"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.project_name}/${var.environment}/*"
      },
      {
        Sid    = "KMSSecretDecryption"
        Effect = "Allow"
        Action = [
          "kms:Decrypt"
        ]
        Resource = var.app_kms_key_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = aws_iam_policy.ecs_execution_policy.arn
}

# 2. ECS API Task Role (Runtime API Workload)
resource "aws_iam_role" "ecs_api_task_role" {
  name = "${var.project_name}-ecs-api-task-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "ecs_api_task_policy" {
  name = "${var.project_name}-ecs-api-task-policy-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3DocumentReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ]
        Resource = [
          var.s3_documents_bucket_arn,
          "${var.s3_documents_bucket_arn}/*"
        ]
      },
      {
        Sid    = "KMSApplicationRuntimeCrypto"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = var.app_kms_key_arn
      },
      {
        Sid    = "OpenSearchSearchAccess"
        Effect = "Allow"
        Action = [
          "es:ESHttpGet",
          "es:ESHttpPost",
          "es:ESHttpPut"
        ]
        Resource = "${var.opensearch_domain_arn}/*"
      },
      {
        Sid    = "SecretsManagerRuntimeAccess"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.project_name}/${var.environment}/*"
      },
      {
        Sid    = "CloudWatchMetricsPut"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace": "EnterpriseRAG/API"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_api_task" {
  role       = aws_iam_role.ecs_api_task_role.name
  policy_arn = aws_iam_policy.ecs_api_task_policy.arn
}

# 3. ECS Worker Task Role (Runtime Background Tasks & Ingestion)
resource "aws_iam_role" "ecs_worker_task_role" {
  name = "${var.project_name}-ecs-worker-task-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "ecs_worker_task_policy" {
  name = "${var.project_name}-ecs-worker-task-policy-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3DocumentReadWriteDelete"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          var.s3_documents_bucket_arn,
          "${var.s3_documents_bucket_arn}/*"
        ]
      },
      {
        Sid    = "KMSApplicationRuntimeCrypto"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = var.app_kms_key_arn
      },
      {
        Sid    = "OpenSearchBulkIndexingAccess"
        Effect = "Allow"
        Action = [
          "es:ESHttp*"
        ]
        Resource = "${var.opensearch_domain_arn}/*"
      },
      {
        Sid    = "SecretsManagerRuntimeAccess"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.project_name}/${var.environment}/*"
      },
      {
        Sid    = "CloudWatchWorkerMetricsPut"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace": "EnterpriseRAG/Worker"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_worker_task" {
  role       = aws_iam_role.ecs_worker_task_role.name
  policy_arn = aws_iam_policy.ecs_worker_task_policy.arn
}

# 4. Metrics Publisher Task Role (Independent Queue Metrics)
resource "aws_iam_role" "ecs_metrics_task_role" {
  name = "${var.project_name}-ecs-metrics-task-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "ecs_metrics_task_policy" {
  name = "${var.project_name}-ecs-metrics-task-policy-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchQueueMetricsPut"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace": "EnterpriseRAG/Tasks"
          }
        }
      },
      {
        Sid    = "SecretsManagerDBSecretRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.project_name}/${var.environment}/database*"
      },
      {
        Sid    = "KMSDBSecretDecryption"
        Effect = "Allow"
        Action = [
          "kms:Decrypt"
        ]
        Resource = var.app_kms_key_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_metrics_task" {
  role       = aws_iam_role.ecs_metrics_task_role.name
  policy_arn = aws_iam_policy.ecs_metrics_task_policy.arn
}

# 5. Migration Task Role (Alembic Schema Migrations)
resource "aws_iam_role" "migration_task_role" {
  name = "${var.project_name}-migration-task-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "migration_task_policy" {
  name = "${var.project_name}-migration-task-policy-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerDBSecretRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.project_name}/${var.environment}/database*"
      },
      {
        Sid    = "KMSDBSecretDecryption"
        Effect = "Allow"
        Action = [
          "kms:Decrypt"
        ]
        Resource = var.app_kms_key_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "migration_task" {
  role       = aws_iam_role.migration_task_role.name
  policy_arn = aws_iam_policy.migration_task_policy.arn
}
