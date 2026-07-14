output "inbound_mx_record" {
  description = "Publish at DNS for the hail-mail base domain."
  value       = "10 inbound-smtp.${var.region}.amazonaws.com"
}

output "inbound_bucket" {
  description = "Confirms $${HAIL_MAIL_NAME_PREFIX}-mail; set HAIL_MAIL_NAME_PREFIX (not this value directly) in .env."
  value       = aws_s3_bucket.inbound.bucket
}

output "lambda_function_arn" {
  value = aws_lambda_function.ingest.arn
}

output "ingest_dlq_url" {
  description = "SQS DLQ for SES notifications the Lambda failed to deliver."
  value       = aws_sqs_queue.ingest_dlq.url
}

output "receipt_rule_set_name" {
  description = "Created but NOT activated — see activate_command."
  value       = aws_ses_receipt_rule_set.main.rule_set_name
}

output "activate_command" {
  description = "Run once per AWS account, after confirming no other rule set is active."
  value       = "aws sesv2 set-active-receipt-rule-set --rule-set-name ${aws_ses_receipt_rule_set.main.rule_set_name}"
}

output "iam_policy_arn" {
  value = aws_iam_policy.main.arn
}

output "iam_access_key_id" {
  description = "Paste into .env as AWS_ACCESS_KEY_ID."
  value       = aws_iam_access_key.main.id
}

output "iam_secret_access_key" {
  description = "Paste into .env as AWS_SECRET_ACCESS_KEY."
  value       = aws_iam_access_key.main.secret
  sensitive   = true
}

output "ses_configuration_set_name" {
  value = aws_sesv2_configuration_set.events.configuration_set_name
}
