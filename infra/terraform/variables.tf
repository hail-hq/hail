variable "name_prefix" {
  description = "Prefix for AWS resources (e.g. hail-inbound-prod). Buckets, the rule set, the Lambda, and IAM roles all derive their names from this."
  type        = string
}

variable "region" {
  description = "AWS region for SES inbound. SES inbound is only available in select regions (us-east-1, us-west-2, eu-west-1, ...). Pick one and stick with it."
  type        = string
}

variable "hail_api_url" {
  description = "Base URL of the Hail API (e.g. https://api.hail.so). No trailing slash."
  type        = string
}

variable "hail_inbound_hmac_secret" {
  description = "Shared secret between the Lambda and the Hail API. Generate something random and 64+ hex chars. Set HAIL_INBOUND_HMAC_SECRET in the API service env to the same value."
  type        = string
  sensitive   = true
}

variable "hail_mail_base_domain" {
  description = "Base domain mail will be received on (e.g. mail.hail.so). Must be in the SES Receipt Rule's recipient filter."
  type        = string
}

variable "raw_object_expiration_days" {
  description = "S3 lifecycle expiration on raw MIME and attachment objects."
  type        = number
  default     = 90
}
