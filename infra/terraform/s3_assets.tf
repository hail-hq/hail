# Shared private object storage. Retention is scoped by object type.
resource "aws_s3_bucket" "assets" {
  bucket = "${var.name_prefix}-assets"
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    id     = "recording-retention"
    status = "Enabled"
    filter { prefix = "recordings/" }
    expiration { days = 365 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  dynamic "rule" {
    for_each = toset([7, 30])
    content {
      id     = "recording-retention-${rule.value}d"
      status = "Enabled"
      filter { prefix = "recordings/${rule.value}d/" }
      expiration { days = rule.value }
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  cors_rule {
    allowed_headers = ["Range"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = var.asset_allowed_origins
    expose_headers  = ["Accept-Ranges", "Content-Length", "Content-Range", "ETag"]
    max_age_seconds = 300
  }
}

resource "aws_iam_policy" "recordings" {
  name = "${var.name_prefix}-recordings"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
      Resource = "${aws_s3_bucket.assets.arn}/recordings/*"
    }]
  })
}

# Dedicated user: its key is handed to LiveKit Cloud Egress, so it must not
# carry the service user's SES or mail-bucket rights.
resource "aws_iam_user" "recordings" {
  name = "${var.name_prefix}-recordings"
}

resource "aws_iam_user_policy_attachment" "recordings" {
  user       = aws_iam_user.recordings.name
  policy_arn = aws_iam_policy.recordings.arn
}

resource "aws_iam_access_key" "recordings" {
  user = aws_iam_user.recordings.name
}

variable "asset_allowed_origins" {
  type    = list(string)
  default = ["https://hail.so"]
}

output "assets_bucket" { value = aws_s3_bucket.assets.bucket }
output "recordings_policy_arn" { value = aws_iam_policy.recordings.arn }

output "recordings_access_key_id" {
  description = "Paste into .env as S3_ACCESS_KEY."
  value       = aws_iam_access_key.recordings.id
}

output "recordings_secret_access_key" {
  description = "Paste into .env as S3_SECRET_KEY."
  value       = aws_iam_access_key.recordings.secret
  sensitive   = true
}
