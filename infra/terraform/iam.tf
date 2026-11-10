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
    sid       = "OutboundAttachmentsReadWriteDelete"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.inbound.arn}/outbound-attachments/*"]
  }

  # The upload-link sweep lists an expired link's key to delete what a
  # cut-off PUT left (each PUT writes <key>.<attempt>; routes/uploads.py).
  statement {
    sid       = "OutboundAttachmentsList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.inbound.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["outbound-attachments/*"]
    }
  }

  # Consent screenshots for US SMS registration. Carrier reviewers open
  # them through the API's public link (routes/sms_evidence.py).
  statement {
    sid       = "SmsEvidenceReadWriteDelete"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.inbound.arn}/sms-evidence/*"]
  }

  # Verification documents sent with an upload link (routes/uploads.py).
  # Read once by POST /verifications, then deleted; the GC deletes any left.
  statement {
    sid       = "VerificationUploadsReadWriteDelete"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.inbound.arn}/verification-uploads/*"]
  }

  # The retention sweep lists a closed organization's folder to delete it,
  # and the upload-link sweep an expired link's attempts.
  statement {
    sid       = "VerificationUploadsList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.inbound.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["verification-uploads/*"]
    }
  }

  # The retention sweep lists a closed organization's evidence to delete it.
  statement {
    sid       = "SmsEvidenceList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.inbound.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["sms-evidence/*"]
    }
  }

  statement {
    sid = "OutboundSES"
    actions = [
      "ses:SendEmail",
      "ses:SendRawEmail",
      "ses:CreateEmailIdentity",
      "ses:GetEmailIdentity",
      "ses:DeleteEmailIdentity",
      # Custom MAIL FROM (send.<domain>) — set by create_identity so the
      # Return-Path aligns to the customer's domain (no "via amazonses.com").
      "ses:PutEmailIdentityMailFromAttributes",
    ]
    resources = ["*"]
  }

  # The voicebot's LLMs on Bedrock (ANTHROPIC_MODEL with
  # ANTHROPIC_BEDROCK_REGION set). Any model and any of the account's
  # inference profiles, so changing ANTHROPIC_MODEL needs no apply. A profile
  # like "eu." routes each request to a region in its geography, so every
  # region is allowed.
  statement {
    sid     = "VoiceLLMBedrock"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    ]
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
