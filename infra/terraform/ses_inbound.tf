resource "aws_ses_receipt_rule_set" "main" {
  rule_set_name = local.rule_set_name
}

resource "aws_ses_receipt_rule" "main" {
  name          = local.rule_name
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  enabled       = true
  scan_enabled  = true
  # Catch-all (no `recipients`): SES accepts mail for ANY verified identity in
  # this account — the hail-mail base domain and every verified custom sender
  # domain. Routing to the right org/domain happens in the ingest layer
  # (email_ingest._find_domain_for_recipient), so no per-domain SES rule is
  # needed and the 200-rule / 100-recipient receipt-rule limits never bind.
  tls_policy = "Require"

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
