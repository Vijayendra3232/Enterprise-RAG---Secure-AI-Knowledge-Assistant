output "domain_id" {
  description = "OpenSearch Domain ID"
  value       = aws_opensearch_domain.search.domain_id
}

output "domain_name" {
  description = "OpenSearch Domain Name"
  value       = aws_opensearch_domain.search.domain_name
}

output "domain_arn" {
  description = "OpenSearch Domain ARN"
  value       = aws_opensearch_domain.search.arn
}

output "endpoint" {
  description = "OpenSearch Domain Endpoint"
  value       = aws_opensearch_domain.search.endpoint
}
