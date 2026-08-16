# Terraform remote state — Honeywell client environment
# State is stored in the same Digilux state bucket under a separate key.
# Prerequisite: same S3 bucket + DynamoDB lock table as digilux-backend.hcl.

bucket         = "digilux-terraform-state"
key            = "delete-account/honeywell/terraform.tfstate"
region         = "ap-south-1"
encrypt        = true
dynamodb_table = "terraform-state-lock"
