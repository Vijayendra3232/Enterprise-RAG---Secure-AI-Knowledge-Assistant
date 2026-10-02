# CloudWatch Logs and Alarms Monitoring Subsystem

locals {
  log_groups = [
    "/ecs/${var.project_name}-api-${var.environment}",
    "/ecs/${var.project_name}-worker-${var.environment}",
    "/ecs/${var.project_name}-metrics-publisher-${var.environment}",
    "/ecs/${var.project_name}-migration-${var.environment}"
  ]
}

resource "aws_cloudwatch_log_group" "ecs_logs" {
  for_each          = toset(local.log_groups)
  name              = each.value
  retention_in_days = var.environment == "production" ? 90 : 30
  kms_key_id        = var.kms_master_key_arn

  tags = {
    Name = "${var.project_name}-log-group-${var.environment}"
  }
}

# 1. ALB High 5xx Error Rate Alarm
resource "aws_cloudwatch_metric_alarm" "alb_5xx_errors" {
  alarm_name          = "${var.project_name}-alb-5xx-high-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "Triggered when ALB target 5xx error count exceeds 10 per minute."

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }
}

# 2. Worker Oldest Task Age Backlog Alarm
resource "aws_cloudwatch_metric_alarm" "worker_task_age_backlog" {
  alarm_name          = "${var.project_name}-task-queue-backlog-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "rag_task_oldest_age_seconds"
  namespace           = "EnterpriseRAG/Tasks"
  period              = 60
  statistic           = "Maximum"
  threshold           = 300 # 5 minutes max task wait threshold
  alarm_description   = "Triggered when oldest pending task in queue exceeds 5 minutes without processing."

  dimensions = {
    Environment = var.environment
    TaskType    = "ALL"
  }
}

# 3. RDS High CPU Utilization Alarm
resource "aws_cloudwatch_metric_alarm" "rds_high_cpu" {
  alarm_name          = "${var.project_name}-rds-high-cpu-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Triggered when RDS PostgreSQL CPU utilization exceeds 80%."

  dimensions = {
    DBInstanceIdentifier = var.db_instance_id
  }
}
