output "ecs_execution_role_arn" {
  description = "ECS Task Execution Role ARN"
  value       = aws_iam_role.ecs_execution_role.arn
}

output "ecs_api_task_role_arn" {
  description = "ECS API Task Role ARN"
  value       = aws_iam_role.ecs_api_task_role.arn
}

output "ecs_worker_task_role_arn" {
  description = "ECS Worker Task Role ARN"
  value       = aws_iam_role.ecs_worker_task_role.arn
}

output "ecs_metrics_task_role_arn" {
  description = "ECS Metrics Publisher Task Role ARN"
  value       = aws_iam_role.ecs_metrics_task_role.arn
}

output "migration_task_role_arn" {
  description = "Database Migration Task Role ARN"
  value       = aws_iam_role.migration_task_role.arn
}
