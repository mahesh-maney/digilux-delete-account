# Terraform remote state — Digilux environment
# Prerequisite: S3 bucket and DynamoDB lock table must exist before first init.
#   aws s3 mb s3://digilux-terraform-state --region ap-south-1
#   aws dynamodb create-table \
#     --table-name terraform-state-lock \
#     --attribute-definitions AttributeName=LockID,AttributeType=S \
#     --key-schema AttributeName=LockID,KeyType=HASH \
#     --billing-mode PAY_PER_REQUEST \
#     --region ap-south-1

bucket         = "digilux-terraform-state"
key            = "delete-account/digilux/terraform.tfstate"
region         = "ap-south-1"
encrypt        = true
dynamodb_table = "terraform-state-lock"
