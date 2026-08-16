terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  # Backend is populated at init time via:
  #   terraform init -backend-config=envs/<env>-backend.hcl
  backend "s3" {
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# Current AWS account ID — used in ARN construction throughout
data "aws_caller_identity" "current" {}
