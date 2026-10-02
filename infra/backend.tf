# Encrypted Remote S3 Backend with SSE-KMS and Native S3 State Locking
terraform {
  backend "s3" {
    bucket       = "enterprise-rag-tf-state-staging-157975549966"
    key          = "staging/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    kms_key_id   = "alias/enterprise-rag-tf-state-staging"
    use_lockfile = true
  }
}
