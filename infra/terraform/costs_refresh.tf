# GitHub Actions access to Bedrock for .github/workflows/costs-refresh.yml.
# The workflow gets short-lived credentials through OIDC, so the repo holds no
# AWS key. Only runs on main of this repo can assume the role.

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "costs_refresh_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
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
  name               = "${var.name_prefix}-costs-refresh"
  assume_role_policy = data.aws_iam_policy_document.costs_refresh_trust.json
  # Longer than the job limit in costs-refresh.yml.
  max_session_duration = 14400
}

data "aws_iam_policy_document" "costs_refresh" {
  # Anthropic models only. A profile like "eu." routes each request to a
  # region in its geography, so every region is allowed.
  statement {
    sid     = "InvokeClaude"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*anthropic.claude-*",
    ]
  }

  statement {
    sid       = "ReadInferenceProfiles"
    actions   = ["bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "costs_refresh" {
  name   = "bedrock"
  role   = aws_iam_role.costs_refresh.id
  policy = data.aws_iam_policy_document.costs_refresh.json
}
