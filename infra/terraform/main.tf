provider "aws" {
  region  = var.region
  profile = var.aws_profile != "" ? var.aws_profile : null
}

locals {
  bucket_name    = "${var.name_prefix}-mail"
  rule_set_name  = "${var.name_prefix}-rules"
  rule_name      = "${var.name_prefix}-deliver"
  lambda_name    = "${var.name_prefix}-ingest"
  log_group_name = "/aws/lambda/${var.name_prefix}-ingest"
}

data "aws_caller_identity" "current" {}
