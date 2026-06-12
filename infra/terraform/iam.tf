data "aws_iam_policy_document" "main" {
  statement {
    sid       = "InboundRawRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.inbound.arn}/raw/*"]
  }

  statement {
    sid       = "InboundAttachmentsReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.inbound.arn}/attachments/*"]
  }

  statement {
    sid = "OutboundSES"
    actions = [
      "ses:SendEmail",
      "ses:SendRawEmail",
      "ses:CreateEmailIdentity",
      "ses:GetEmailIdentity",
      "ses:DeleteEmailIdentity",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "main" {
  name   = var.iam_user_name
  policy = data.aws_iam_policy_document.main.json
}

resource "aws_iam_user" "main" {
  name = var.iam_user_name
}

resource "aws_iam_user_policy_attachment" "main" {
  user       = aws_iam_user.main.name
  policy_arn = aws_iam_policy.main.arn
}

resource "aws_iam_access_key" "main" {
  user = aws_iam_user.main.name
}
