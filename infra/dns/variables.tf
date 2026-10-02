variable "root_domain_name" {
  description = "Root domain name for Route 53 hosted zone"
  type        = string
}

variable "domain_name" {
  description = "Full domain name (FQDN) for API record"
  type        = string
}

variable "alb_dns_name" {
  description = "ALB DNS name"
  type        = string
}

variable "alb_zone_id" {
  description = "ALB hosted zone ID"
  type        = string
}
