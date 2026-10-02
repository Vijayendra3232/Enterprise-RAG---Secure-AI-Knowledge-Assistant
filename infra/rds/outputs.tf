output "db_instance_id" {
  description = "RDS DB Instance Identifier"
  value       = aws_db_instance.postgresql.identifier
}

output "db_endpoint" {
  description = "RDS DB connection endpoint"
  value       = aws_db_instance.postgresql.endpoint
}

output "db_address" {
  description = "RDS DB hostname address"
  value       = aws_db_instance.postgresql.address
}

output "db_port" {
  description = "RDS DB port"
  value       = aws_db_instance.postgresql.port
}

output "db_name" {
  description = "RDS DB database name"
  value       = aws_db_instance.postgresql.db_name
}
