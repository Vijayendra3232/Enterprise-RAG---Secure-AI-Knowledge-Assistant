output "alb_security_group_id" {
  description = "ALB Security Group ID"
  value       = aws_security_group.alb.id
}

output "ecs_api_security_group_id" {
  description = "ECS API Security Group ID"
  value       = aws_security_group.ecs_api.id
}

output "ecs_worker_security_group_id" {
  description = "ECS Worker Security Group ID"
  value       = aws_security_group.ecs_worker.id
}

output "ecs_metrics_security_group_id" {
  description = "ECS Metrics Publisher Security Group ID"
  value       = aws_security_group.ecs_metrics.id
}

output "migration_security_group_id" {
  description = "Migration Security Group ID"
  value       = aws_security_group.migration.id
}

output "rds_security_group_id" {
  description = "RDS PostgreSQL Security Group ID"
  value       = aws_security_group.rds.id
}

output "opensearch_security_group_id" {
  description = "OpenSearch Security Group ID"
  value       = aws_security_group.opensearch.id
}
