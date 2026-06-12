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
