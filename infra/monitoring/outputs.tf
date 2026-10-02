output "log_group_arns" {
  description = "ARNs of ECS CloudWatch log groups"
  value       = [for lg in aws_cloudwatch_log_group.ecs_logs : lg.arn]
}

output "alb_alarm_arn" {
  description = "ALB 5xx metric alarm ARN"
  value       = aws_cloudwatch_metric_alarm.alb_5xx_errors.arn
}

output "worker_alarm_arn" {
  description = "Worker queue backlog metric alarm ARN"
  value       = aws_cloudwatch_metric_alarm.worker_task_age_backlog.arn
}

output "rds_alarm_arn" {
  description = "RDS high CPU metric alarm ARN"
  value       = aws_cloudwatch_metric_alarm.rds_high_cpu.arn
}
