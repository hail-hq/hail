data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../ses-ingest-lambda"
  output_path = "${path.module}/.build/ses-ingest-lambda.zip"
  excludes    = ["test_handler.py", "README.md"]
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = local.log_group_name
  retention_in_days = 30
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest" {
  name               = "${local.lambda_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "ingest_logs" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Failed async invokes (Hail API unreachable past Lambda's built-in retries)
# land here instead of vanishing. Raw MIME is already safe in S3; this queue
# preserves the SES notification for replay.
resource "aws_sqs_queue" "ingest_dlq" {
  name                      = "${local.lambda_name}-dlq"
  message_retention_seconds = 1209600 # 14 days
}

data "aws_iam_policy_document" "ingest_dlq" {
  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest_dlq.arn]
  }
}

resource "aws_iam_role_policy" "ingest_dlq" {
  name   = "${local.lambda_name}-dlq"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest_dlq.json
}

resource "aws_lambda_function" "ingest" {
  function_name    = local.lambda_name
  role             = aws_iam_role.ingest.arn
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  runtime          = "python3.12"
  handler          = "handler.handler"
  timeout          = 15
  memory_size      = 256

  dead_letter_config {
    target_arn = aws_sqs_queue.ingest_dlq.arn
  }

  environment {
    variables = {
      HAIL_API_URL             = var.hail_api_url
      HAIL_INBOUND_BUCKET      = aws_s3_bucket.inbound.bucket
      HAIL_INBOUND_HMAC_SECRET = var.hail_inbound_hmac_secret
    }
  }

  # Lambda validates SendMessage access to the DLQ at create time, so the
  # role policy must exist first.
  depends_on = [aws_cloudwatch_log_group.ingest, aws_iam_role_policy.ingest_dlq]
}

resource "aws_lambda_permission" "ses_invoke" {
  statement_id   = "AllowSESInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.ingest.function_name
  principal      = "ses.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
}
