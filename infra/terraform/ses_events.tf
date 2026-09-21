# SES configuration set → SNS → ingest Lambda → POST /internal/ses-events.
# Open/Click tracking uses the default SES tracking domain unless
# var.ses_tracking_domain is set. SES only accepts a custom redirect domain
# that is a verified identity, so the identity is created here; publish its
# DKIM records (output `ses_tracking_domain_dkim_records`) before it verifies.

resource "aws_sesv2_email_identity" "tracking" {
  count          = var.ses_tracking_domain == "" ? 0 : 1
  email_identity = var.ses_tracking_domain
}

resource "aws_sesv2_configuration_set" "events" {
  configuration_set_name = var.ses_configuration_set_name

  dynamic "tracking_options" {
    for_each = var.ses_tracking_domain == "" ? [] : [var.ses_tracking_domain]
    content {
      custom_redirect_domain = tracking_options.value
      https_policy           = "REQUIRE"
    }
  }

  depends_on = [aws_sesv2_email_identity.tracking]
}

resource "aws_sns_topic" "ses_events" {
  name = "${var.name_prefix}-ses-events"
}

# Without this, SES's default topic policy may not admit publishes from the
# config-set event destination — events would be silently dropped (nothing
# reaches the Lambda or the DLQ, no error surfaces anywhere).
resource "aws_sns_topic_policy" "ses_events" {
  arn = aws_sns_topic.ses_events.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.ses_events.arn
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
        ArnLike = {
          "aws:SourceArn" = aws_sesv2_configuration_set.events.arn
        }
      }
    }]
  })
}

resource "aws_sesv2_configuration_set_event_destination" "sns" {
  configuration_set_name = aws_sesv2_configuration_set.events.configuration_set_name
  event_destination_name = "sns"

  event_destination {
    enabled = true
    matching_event_types = [
      "DELIVERY", "BOUNCE", "COMPLAINT", "REJECT",
      "DELIVERY_DELAY", "OPEN", "CLICK",
    ]
    sns_destination {
      topic_arn = aws_sns_topic.ses_events.arn
    }
  }
}

resource "aws_sqs_queue" "ses_events_dlq" {
  name                      = "${var.name_prefix}-ses-events-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue_policy" "ses_events_dlq" {
  queue_url = aws_sqs_queue.ses_events_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.ses_events_dlq.arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_sns_topic.ses_events.arn }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "ses_events_lambda" {
  topic_arn = aws_sns_topic.ses_events.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ingest.arn

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ses_events_dlq.arn
  })
}

resource "aws_lambda_permission" "sns_ses_events" {
  statement_id  = "AllowSNSSesEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.ses_events.arn
}
