output "cluster_id" {
  description = "ECS Cluster ID"
  value       = aws_ecs_cluster.main.id
}

output "cluster_name" {
  description = "ECS Cluster Name"
  value       = aws_ecs_cluster.main.name
}

output "api_service_name" {
  description = "ECS API Service Name"
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "ECS Worker Service Name"
  value       = aws_ecs_service.worker.name
}

output "metrics_publisher_service_name" {
  description = "ECS Metrics Publisher Service Name"
  value       = aws_ecs_service.metrics_publisher.name
}

output "api_task_definition_arn" {
  description = "ECS API Task Definition ARN"
  value       = aws_ecs_task_definition.api.arn
}

output "worker_task_definition_arn" {
  description = "ECS Worker Task Definition ARN"
  value       = aws_ecs_task_definition.worker.arn
}

output "migration_task_definition_arn" {
  description = "ECS Migration Task Definition ARN"
  value       = aws_ecs_task_definition.migration.arn
}
