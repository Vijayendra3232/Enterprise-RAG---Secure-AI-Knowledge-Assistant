output "fqdn" {
  description = "Fully Qualified Domain Name of API"
  value       = aws_route53_record.api.fqdn
}
