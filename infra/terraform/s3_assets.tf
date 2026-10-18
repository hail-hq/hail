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
    expiration { days = var.recording_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
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

variable "recording_retention_days" {
  type    = number
  default = 30
  validation {
    condition     = var.recording_retention_days >= 1
    error_message = "Recording retention must be at least one day."
  }
}

variable "asset_allowed_origins" {
  type    = list(string)
  default = ["https://hail.so"]
}

output "assets_bucket" { value = aws_s3_bucket.assets.bucket }
output "recordings_policy_arn" { value = aws_iam_policy.recordings.arn }
