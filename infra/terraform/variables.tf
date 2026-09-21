variable "name_prefix" {
  description = "Prefix for AWS resources (S3 bucket, SES rule set, Lambda, IAM)."
  type        = string
}

variable "region" {
  description = "AWS region. Must support SES inbound (us-east-1, us-west-2, eu-west-1, ...)."
  type        = string
}

variable "hail_api_url" {
  description = "Public base URL of the Hail API. The Lambda POSTs to <hail_api_url>/internal/ses-events."
  type        = string
}

variable "hail_inbound_hmac_secret" {
  description = "Shared HMAC secret between the Lambda and the API."
  type        = string
  sensitive   = true
}

variable "hail_mail_base_domain" {
  description = "Domain SES receives mail on (e.g. mail.hail.so)."
  type        = string
}

variable "raw_object_expiration_days" {
  description = "S3 lifecycle expiration on raw MIME and attachment objects."
  type        = number
  default     = 90
}

variable "lambda_source_dir" {
  description = "Absolute path to the ses-ingest-lambda directory. Set by Terragrunt; default works when running `terraform` directly from infra/terraform/."
  type        = string
  default     = ""
}

variable "aws_profile" {
  description = "AWS named profile for the provider. Empty uses the default credential chain."
  type        = string
  default     = ""
}

variable "iam_user_name" {
  description = "IAM user provisioned for every Hail service."
  type        = string
  default     = "hail"
}

variable "ses_configuration_set_name" {
  description = "SES configuration set attached to outbound sends (must match HAIL_SES_CONFIGURATION_SET in the API env)."
  type        = string
  default     = "hail-events"
}

variable "ses_tracking_domain" {
  description = "Host name for open/click tracking links (e.g. go.example.com). Empty keeps the default SES tracking domain. Set it only after the HTTPS proxy for this host answers (see docs/public/self-host/aws-ses.md, \"Tracking domain\")."
  type        = string
  default     = ""

  validation {
    condition     = var.ses_tracking_domain == "" || can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.ses_tracking_domain))
    error_message = "ses_tracking_domain must be a bare lowercase host name such as go.example.com (no https://, path, or spaces)."
  }
}
