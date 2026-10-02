# Terraform Backend Bootstrap

This isolated module provisions the foundational infrastructure required for remote Terraform state management:

1. **Dedicated Terraform State KMS CMK**: Customer-managed key with automatic key rotation and strict key policies (Admin/Deployment access only, denying ECS runtime access).
2. **S3 Remote State Bucket**: Private bucket with Block Public Access, Bucket Owner Enforced (no ACLs), object versioning, and server-side encryption via the dedicated State CMK.
3. **State Locking**: Enforced via modern S3-native state locking.

---

## Bootstrap Workflow

```text
1. Authenticate to AWS CLI:
   aws sts get-caller-identity

2. Deploy Bootstrap Stack:
   cd infra/bootstrap
   terraform init
   terraform apply -var="environment=production"

3. Verify Created State Resources:
   aws s3api get-public-access-block --bucket <state-bucket-name>
   aws kms describe-key --key-id <state-kms-key-arn>

4. Configure Main Backend:
   Copy the `backend_config_snippet` output into `infra/backend.tf`.

5. Initialize Main Infrastructure:
   cd ../
   terraform init
   terraform plan -var-file="terraform.tfvars"
```

> [!IMPORTANT]
> This bootstrap module must be deployed once per environment (`dev`, `staging`, `production`) and is lifecycle-protected (`prevent_destroy = true`) to prevent accidental destruction during application infrastructure teardown.
