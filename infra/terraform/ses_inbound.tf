resource "aws_ses_receipt_rule_set" "main" {
  rule_set_name = local.rule_set_name
}

resource "aws_ses_receipt_rule" "main" {
  name          = local.rule_name
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  enabled       = true
  scan_enabled  = true
  # Catch-all (no `recipients`): SES accepts mail for ANY verified identity in
  # this account: the hail-mail base domain and every verified custom sender
  # domain. Routing to the right org/domain happens in the ingest layer
  # (email_ingest._find_domain_for_recipient), so no per-domain SES rule is
  # needed and the 200-rule / 100-recipient receipt-rule limits never bind.
  #
  # RULE ORDER. SES runs the active rule set top to bottom and a rule without
  # `after` is inserted at the TOP at creation time. Other stacks in the same
  # AWS account may place narrower rules above this catch-all (for example the
  # hail.so website's email delivery checker, which receives on its own
  # subdomain and ends with a stop action). Those rules only work while they
  # sit above this one. Recreating this rule (rename, taint, replace) would
  # insert it at the top again and silently swallow their mail, with no error
  # anywhere. `prevent_destroy` blocks that. If this rule must be recreated,
  # remove the guard on purpose, apply, then run
  #   aws ses describe-active-receipt-rule-set --query 'Rules[].Name' --output text
  # and move this rule back to the bottom with
  #   aws ses set-receipt-rule-position --rule-set-name <set> --rule-name <this> --after <last other rule>
  tls_policy = "Require"

  lifecycle {
    prevent_destroy = true
  }

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
