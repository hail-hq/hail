# The workflow gets short-lived credentials through GitHub OIDC, so the repo
# holds no AWS key. Only runs on main of hail-hq/hail can assume the role, and
# the role can only call the one model the workflow uses.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  description = "AWS region for the provider. IAM is global."
  type        = string
}

variable "aws_profile" {
  description = "Local AWS profile. Empty uses the default credential chain."
  type        = string
  default     = ""
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile != "" ? var.aws_profile : null
}

data "aws_caller_identity" "current" {}

# The account already has GitHub's OIDC provider, and AWS allows one per URL,
# so this stack reads it instead of creating it.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:hail-hq/hail:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "costs_refresh" {
  name               = "hail-costs-refresh"
  assume_role_policy = data.aws_iam_policy_document.trust.json
  # Longer than the job limit in costs-refresh.yml.
  max_session_duration = 14400
}

data "aws_iam_policy_document" "bedrock" {
  # The "eu." profile routes each request to a region in its geography, so
  # every region is allowed.
  statement {
    sid     = "InvokeSonnet"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5-5*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/eu.anthropic.claude-sonnet-5-5",
    ]
  }

  statement {
    sid       = "ReadInferenceProfiles"
    actions   = ["bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "bedrock"
  role   = aws_iam_role.costs_refresh.id
  policy = data.aws_iam_policy_document.bedrock.json
}

output "role_arn" {
  description = "Set as the AWS_ROLE_TO_ASSUME repo secret."
  value       = aws_iam_role.costs_refresh.arn
}
