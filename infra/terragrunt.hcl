# Terragrunt wrapper around the inbound-email Terraform module.
#
# Reads operator config from the repo's .env directly via run_cmd —
# no need to `set -a; source .env; set +a` first. Just:
#
#   terragrunt apply
#
# AWS credentials come from the profile named in $AWS_PROFILE in .env.
# The S3 backend block below reads the same .env, so backend auth and
# module auth stay consistent.
#
# State + infra regions are decoupled. HAIL_TERRAFORM_STATE_REGION is
# where the S3 state bucket and DynamoDB lock table live (typically a
# single shared region across all your deployments); AWS_REGION is
# where the SES + Lambda + raw-MIME S3 bucket get provisioned (often
# per-deployment). If you don't set HAIL_TERRAFORM_STATE_REGION, it
# falls back to AWS_REGION (single-region setups stay one var).
#
# One-time bootstrap (per AWS account, before the first `terragrunt
# init`) — the state bucket + lock table can't bootstrap themselves:
#
#   set -a; source .env; set +a
#   STATE_REGION=${HAIL_TERRAFORM_STATE_REGION:-$AWS_REGION}
#   aws --profile $AWS_PROFILE s3api create-bucket \
#       --bucket $HAIL_TERRAFORM_STATE_BUCKET \
#       --region $STATE_REGION
#   aws --profile $AWS_PROFILE s3api put-bucket-versioning \
#       --bucket $HAIL_TERRAFORM_STATE_BUCKET \
#       --versioning-configuration Status=Enabled
#   aws --profile $AWS_PROFILE dynamodb create-table \
#       --table-name $HAIL_TERRAFORM_LOCK_TABLE \
#       --attribute-definitions AttributeName=LockID,AttributeType=S \
#       --key-schema AttributeName=LockID,KeyType=HASH \
#       --billing-mode PAY_PER_REQUEST \
#       --region $STATE_REGION

locals {
  env_file = "${get_repo_root()}/.env"

  # Sourced from .env. HCL only interprets `${...}` with braces — bare
  # `$VAR` passes through to bash unchanged, so no escape needed for
  # those. For `${VAR:-default}` we escape the HCL braces with `$${...}`
  # so bash gets the parameter-expansion form. Empty AWS_PROFILE means
  # "use the default AWS credential chain" — leave it that way for
  # IAM-role hosts.
  aws_profile  = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${AWS_PROFILE:-}\"")
  aws_region   = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${AWS_REGION:-us-east-1}\"")
  state_region = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${HAIL_TERRAFORM_STATE_REGION:-$${AWS_REGION:-us-east-1}}\"")
  state_bucket = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_TERRAFORM_STATE_BUCKET\"")
  lock_table   = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_TERRAFORM_LOCK_TABLE\"")
  # Prefix for the AWS resources THIS Terraform module owns
  # (SES + S3 + Lambda for inbound email). Scoped to email because
  # other inbound channels (SMS, voice) don't provision AWS infra.
  email_inbound_name_prefix = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${HAIL_INBOUND_EMAIL_NAME_PREFIX:-hail-email-inbound}\"")
  hail_api_url              = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_API_URL\"")
  hail_inbound_secret       = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_INBOUND_HMAC_SECRET\"")
  hail_mail_base_domain     = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_MAIL_BASE_DOMAIN\"")
}

terraform {
  source = "./terraform"
}

remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    bucket         = local.state_bucket
    key            = "${path_relative_to_include()}/inbound-email.tfstate"
    region         = local.state_region
    encrypt        = true
    dynamodb_table = local.lock_table
    profile        = local.aws_profile
  }
}

inputs = {
  name_prefix              = local.email_inbound_name_prefix
  region                   = local.aws_region
  hail_api_url             = local.hail_api_url
  hail_inbound_hmac_secret = local.hail_inbound_secret
  hail_mail_base_domain    = local.hail_mail_base_domain
  # AWS profile for the provider — same value the S3 backend above
  # uses so backend + resource calls share one credential source.
  aws_profile = local.aws_profile
  # Absolute path to the Lambda source. Terragrunt copies `./terraform`
  # into `.terragrunt-cache/`, so the in-tree relative reference
  # (`${path.module}/../ses-ingest-lambda`) breaks; thread the real
  # repo-rooted path through as a variable.
  lambda_source_dir = "${get_repo_root()}/infra/ses-ingest-lambda"
}
