output "alb_id" {
  description = "ALB ID"
  value       = aws_lb.api.id
}

output "alb_arn" {
  description = "ALB ARN"
  value       = aws_lb.api.arn
}

output "alb_arn_suffix" {
  description = "ALB ARN suffix for CloudWatch alarms"
  value       = aws_lb.api.arn_suffix
}

output "alb_dns_name" {
  description = "ALB DNS Hostname"
  value       = aws_lb.api.dns_name
}

output "alb_zone_id" {
  description = "ALB Canonical Hosted Zone ID"
  value       = aws_lb.api.zone_id
}

output "target_group_arn" {
  description = "ALB Target Group ARN"
  value       = aws_lb_target_group.api.arn
}

output "target_group_name" {
  description = "ALB Target Group Name"
  value       = aws_lb_target_group.api.name
}

output "https_listener_arn" {
  description = "HTTPS Listener ARN"
  value       = aws_lb_listener.https.arn
}
