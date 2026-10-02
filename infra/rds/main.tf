# Amazon RDS PostgreSQL Multi-AZ Deployment
resource "aws_db_subnet_group" "rds" {
  name        = "${var.project_name}-db-subnet-group-${var.environment}"
  description = "Subnet group for multi-AZ RDS PostgreSQL"
  subnet_ids  = var.subnet_ids

  tags = {
    Name = "${var.project_name}-db-subnet-group-${var.environment}"
  }
}

resource "aws_db_parameter_group" "rds" {
  name        = "${var.project_name}-pg-params-${var.environment}"
  family      = "postgres16"
  description = "Custom parameter group enforcing SSL/TLS for PostgreSQL"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = {
    Name = "${var.project_name}-pg-params-${var.environment}"
  }
}

resource "aws_db_instance" "postgresql" {
  identifier                  = "${var.project_name}-db-${var.environment}"
  engine                      = "postgres"
  engine_version              = "16.3"
  instance_class              = var.db_instance_class
  allocated_storage           = var.db_allocated_storage_gb
  max_allocated_storage       = var.db_allocated_storage_gb * 3
  storage_type                = "gp3"
  storage_encrypted           = true
  kms_key_id                  = var.kms_master_key_arn
  multi_az                    = true
  publicly_accessible         = false
  db_subnet_group_name        = aws_db_subnet_group.rds.name
  vpc_security_group_ids      = [var.security_group_id]
  parameter_group_name        = aws_db_parameter_group.rds.name
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false

  db_name  = "enterpriserag"
  username = "ragadmin"
  # Password managed by AWS Secrets Manager or generated dynamically
  manage_master_user_password = true
  master_user_secret_kms_key_id = var.kms_master_key_arn

  backup_retention_period   = 30
  backup_window             = "03:00-04:00"
  maintenance_window        = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot     = true
  deletion_protection       = var.environment == "production" ? true : false
  skip_final_snapshot       = var.environment == "production" ? false : true
  final_snapshot_identifier = "${var.project_name}-db-final-snapshot-${var.environment}"

  tags = {
    Name = "${var.project_name}-db-${var.environment}"
  }
}
