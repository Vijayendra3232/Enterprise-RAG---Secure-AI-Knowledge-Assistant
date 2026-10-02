# Root Outputs for Enterprise RAG AWS Deployment

output "vpc_id" {
  description = "VPC ID"
  value       = module.network.vpc_id
}

output "alb_dns_name" {
  description = "Application Load Balancer DNS Hostname"
  value       = module.alb.alb_dns_name
}

output "s3_documents_bucket_name" {
  description = "S3 Documents Bucket Name"
  value       = module.s3.bucket_name
}

output "rds_endpoint" {
  description = "RDS PostgreSQL Endpoint"
  value       = module.rds.db_endpoint
}

output "opensearch_endpoint" {
  description = "Amazon OpenSearch Service Endpoint"
  value       = module.opensearch.endpoint
}

output "ecs_cluster_name" {
  description = "ECS Cluster Name"
  value       = module.ecs.cluster_name
}

output "ecs_api_service_name" {
  description = "ECS API Service Name"
  value       = module.ecs.api_service_name
}

output "ecs_worker_service_name" {
  description = "ECS Worker Service Name"
  value       = module.ecs.worker_service_name
}

output "ecs_metrics_publisher_service_name" {
  description = "ECS Metrics Publisher Service Name"
  value       = module.ecs.metrics_publisher_service_name
}

output "kms_app_cmk_arn" {
  description = "Application Customer-Managed Key ARN"
  value       = module.kms.app_cmk_arn
}
