resource "aws_s3_bucket" "inbound" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_public_access_block" "inbound" {
  bucket                  = aws_s3_bucket.inbound.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id

  rule {
    id     = "expire-raw"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    expiration {
      days = var.raw_object_expiration_days
    }
  }

  rule {
    id     = "expire-attachments"
    status = "Enabled"
    filter {
      prefix = "attachments/"
    }
    expiration {
      days = var.raw_object_expiration_days
    }
  }

  # Backstop for identity documents: the app deletes them within about an
  # hour (EmailAttachmentGcWorker); S3 removes any it missed after a day.
  rule {
    id     = "expire-verification-uploads"
    status = "Enabled"
    filter {
      prefix = "verification-uploads/"
    }
    expiration {
      days = 1
    }
  }

  # Backstop for the copies of registration files kept for a resubmit: the
  # app deletes them 30 days after submit at the latest (the verification
  # worker); S3 removes any it missed after 32 days.
  rule {
    id     = "expire-verification-kept"
    status = "Enabled"
    filter {
      prefix = "verification-kept/"
    }
    expiration {
      days = 32
    }
  }

  # Knowledge files a browser sends straight to storage
  # (routes/knowledge_uploads.py): parts of a multipart upload never
  # finished are dropped after a day. No expiration: a document's file is
  # kept while the document lives.
  rule {
    id     = "abort-knowledge-multipart"
    status = "Enabled"
    filter {
      prefix = "knowledge-documents/"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  # No bucket-wide expiration rule for outbound-attachments/: those objects
  # are retained per the app-level contract (used attachments are kept
  # indefinitely and reusable across sends; only never-used uploads expire,
  # 24h after upload, via EmailAttachmentGcWorker). A blanket time-based S3
  # rule here would silently delete objects still backing sent emails.
}

# The console sends knowledge files straight to the bucket with URLs the API
# presigns (routes/knowledge_uploads.py, Uppy's @uppy/aws-s3). A presigned
# URL is the only credential; CORS only lets the console's pages read the
# answers (ETag of each part). Same origins as the assets bucket.
resource "aws_s3_bucket_cors_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  cors_rule {
    allowed_headers = ["content-type"]
    allowed_methods = ["PUT", "POST", "GET", "DELETE"]
    allowed_origins = var.asset_allowed_origins
    expose_headers  = ["ETag", "Location"]
    max_age_seconds = 3000
  }
}

data "aws_iam_policy_document" "ses_write" {
  statement {
    sid     = "AllowSESPuts"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.inbound.arn}/raw/*",
    ]
    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  policy = data.aws_iam_policy_document.ses_write.json
}
