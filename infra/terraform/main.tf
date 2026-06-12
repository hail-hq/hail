provider "aws" {
  region = var.region
  # Empty string is the AWS provider's idiom for "no explicit profile;
  # fall back to env vars / default chain". Terragrunt threads
  # $AWS_PROFILE from .env; plain `terraform apply` leaves it unset.
  profile = var.aws_profile != "" ? var.aws_profile : null
}

locals {
  bucket_name    = "${var.name_prefix}-raw"
  rule_set_name  = "${var.name_prefix}-rules"
  rule_name      = "${var.name_prefix}-deliver"
  lambda_name    = "${var.name_prefix}-ingest"
  log_group_name = "/aws/lambda/${var.name_prefix}-ingest"
}

data "aws_caller_identity" "current" {}
