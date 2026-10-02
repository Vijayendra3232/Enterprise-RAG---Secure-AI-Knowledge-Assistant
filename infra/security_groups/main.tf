# Least-Privilege Security Groups

# 1. Application Load Balancer Security Group
resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg-${var.environment}"
  description = "Controls public inbound HTTPS traffic to the ALB"
  vpc_id      = var.vpc_id

  ingress {
    description = "Public HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Public HTTP (for HTTPS redirect)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description     = "Forward traffic to ECS API tasks"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_api.id]
  }

  tags = {
    Name = "${var.project_name}-alb-sg-${var.environment}"
  }
}

# 2. ECS API Tasks Security Group
resource "aws_security_group" "ecs_api" {
  name        = "${var.project_name}-ecs-api-sg-${var.environment}"
  description = "Allows inbound traffic only from the ALB"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Inbound from ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Outbound to private VPC and endpoints"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ecs-api-sg-${var.environment}"
  }
}

# 3. ECS Worker Tasks Security Group
resource "aws_security_group" "ecs_worker" {
  name        = "${var.project_name}-ecs-worker-sg-${var.environment}"
  description = "No inbound traffic; outbound to RDS, OpenSearch, VPC endpoints, and NAT"
  vpc_id      = var.vpc_id

  egress {
    description = "Outbound to internal services, VPC endpoints, and NAT"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ecs-worker-sg-${var.environment}"
  }
}

# 4. ECS Metrics Publisher Tasks Security Group
resource "aws_security_group" "ecs_metrics" {
  name        = "${var.project_name}-ecs-metrics-sg-${var.environment}"
  description = "No inbound traffic; outbound to RDS and CloudWatch"
  vpc_id      = var.vpc_id

  egress {
    description = "Outbound to RDS and CloudWatch"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ecs-metrics-sg-${var.environment}"
  }
}

# 5. Database Migration Task Security Group
resource "aws_security_group" "migration" {
  name        = "${var.project_name}-migration-sg-${var.environment}"
  description = "One-off task security group for running Alembic migrations"
  vpc_id      = var.vpc_id

  egress {
    description = "Outbound to RDS and Secrets Manager"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-migration-sg-${var.environment}"
  }
}

# 6. RDS PostgreSQL Security Group
resource "aws_security_group" "rds" {
  name        = "${var.project_name}-rds-sg-${var.environment}"
  description = "Allows PostgreSQL access only from API, Worker, Metrics, and Migration tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from API tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_api.id]
  }

  ingress {
    description     = "PostgreSQL from Worker tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_worker.id]
  }

  ingress {
    description     = "PostgreSQL from Metrics Publisher"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_metrics.id]
  }

  ingress {
    description     = "PostgreSQL from Migration tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.migration.id]
  }

  tags = {
    Name = "${var.project_name}-rds-sg-${var.environment}"
  }
}

# 7. Amazon OpenSearch Service Security Group
resource "aws_security_group" "opensearch" {
  name        = "${var.project_name}-opensearch-sg-${var.environment}"
  description = "Allows OpenSearch HTTPS access only from API and Worker tasks"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTPS from API tasks"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_api.id]
  }

  ingress {
    description     = "HTTPS from Worker tasks"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_worker.id]
  }

  tags = {
    Name = "${var.project_name}-opensearch-sg-${var.environment}"
  }
}
