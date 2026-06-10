# SES has a single *active* receipt rule set per region per account.
# This module creates the rule set but does NOT activate it — activation
# is a destructive operation if the account already has one running.
# Activate manually after apply; see docs/setup/aws-ses.md §10.
resource "aws_ses_receipt_rule_set" "hail" {
  rule_set_name = local.rule_set_name
}

resource "aws_ses_receipt_rule" "hail" {
  name          = local.rule_name
  rule_set_name = aws_ses_receipt_rule_set.hail.rule_set_name
  enabled       = true
  scan_enabled  = true
  recipients    = [var.hail_mail_base_domain]
  tls_policy    = "Require"

  s3_action {
    bucket_name       = aws_s3_bucket.inbound.bucket
    object_key_prefix = "raw/"
    position          = 1
  }

  lambda_action {
    function_arn    = aws_lambda_function.ingest.arn
    invocation_type = "Event"
    position        = 2
  }

  depends_on = [
    aws_s3_bucket_policy.inbound,
    aws_lambda_permission.ses_invoke,
  ]
}
