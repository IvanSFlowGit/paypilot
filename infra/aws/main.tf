# PayPilot decision slice on AWS: one HTTP API, one Lambda, one RDS Postgres.
#
# Deliberately absent, each for a stated reason:
# - NAT gateway: the function needs no outbound internet. Secrets are read from
#   SSM at PLAN time by Terraform and passed as encrypted Lambda environment
#   variables, so the function never calls SSM and needs no VPC endpoint.
# - Multi-AZ RDS: doubles the bill to demonstrate a property nobody will ask about.
# - Secrets Manager: per-secret monthly cost for the job SSM SecureString does free.
# - Reserved concurrency: a new account's concurrency limit can be too low to
#   reserve any; API Gateway stage throttling caps traffic instead.
#
# - Master credentials on the public function: the decision function logs in as
#   paypilot_app (SELECT and INSERT only). The master password lives only on the
#   bootstrap function, which no HTTP route reaches.
#
# Trade-off accepted: because Terraform reads the SSM values, they sit in
# plaintext in terraform.tfstate. State is local and gitignored. Treat the state
# file as a secret.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project = var.project
      slice   = "decision"
      managed = "terraform"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ssm_parameter" "db_password" {
  name            = var.db_password_ssm_name
  with_decryption = true
}

data "aws_ssm_parameter" "app_db_password" {
  name            = var.app_db_password_ssm_name
  with_decryption = true
}

data "aws_ssm_parameter" "decision_api_token" {
  name            = var.decision_api_token_ssm_name
  with_decryption = true
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}
