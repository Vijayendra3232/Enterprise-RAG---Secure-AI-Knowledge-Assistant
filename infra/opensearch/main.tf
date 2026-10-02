# Amazon OpenSearch Service High-Availability Multi-AZ Deployment (Correction 3 & 5)
resource "aws_opensearch_domain" "search" {
  domain_name    = "${var.project_name}-search-${var.environment}"
  engine_version = "OpenSearch_2.13"

  cluster_config {
    instance_type          = var.instance_type
    instance_count         = var.instance_count
    zone_awareness_enabled = true

    zone_awareness_config {
      availability_zone_count = length(var.subnet_ids)
    }

    # Dedicated Cluster Manager (Master) Nodes for Quorum and Split-Brain Prevention
    dedicated_master_enabled = true
    dedicated_master_count   = var.dedicated_master_count
    dedicated_master_type    = var.dedicated_master_type
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.volume_size_gb
  }

  # Encryption at Rest with Application KMS CMK
  encrypt_at_rest {
    enabled    = true
    kms_key_id = var.kms_master_key_arn
  }

  # In-Transit Encryption & Modern TLS
  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  # Private VPC Subnets & Security Group
  vpc_options {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.security_group_id]
  }

  # SigV4 IAM Domain Access Policy (Correction 5)
  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSTaskRolesSigV4Access"
        Effect = "Allow"
        Principal = {
          AWS = [
            var.ecs_api_task_role_arn,
            var.ecs_worker_task_role_arn
          ]
        }
        Action   = "es:ESHttp*"
        Resource = "arn:aws:es:${var.aws_region}:${var.account_id}:domain/${var.project_name}-search-${var.environment}/*"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-search-${var.environment}"
  }
}
