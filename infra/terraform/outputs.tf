output "inbound_mx_record" {
  description = "Publish at DNS for the hail-mail base domain. The full record is e.g. '10 inbound-smtp.us-east-1.amazonaws.com'."
  value       = "10 inbound-smtp.${var.region}.amazonaws.com"
}

output "inbound_bucket" {
  description = "Raw-MIME bucket. Set HAIL_INBOUND_BUCKET in the API .env to this value."
  value       = aws_s3_bucket.inbound.bucket
}

output "lambda_function_arn" {
  description = "ARN of the deployed ses-ingest-lambda."
  value       = aws_lambda_function.ingest.arn
}

output "ingest_dlq_url" {
  description = "SQS DLQ holding SES notifications the Lambda failed to deliver to the Hail API"
  value       = aws_sqs_queue.ingest_dlq.url
}

output "receipt_rule_set_name" {
  description = "Created but NOT activated. Activate manually — see activate_command."
  value       = aws_ses_receipt_rule_set.hail.rule_set_name
}

output "activate_command" {
  description = "Run once after apply, in an account with no other active receipt rule set. See docs/setup/aws-ses.md §10."
  value       = "aws sesv2 set-active-receipt-rule-set --rule-set-name ${aws_ses_receipt_rule_set.hail.rule_set_name}"
}
