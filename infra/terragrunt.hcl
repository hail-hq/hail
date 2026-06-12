# Reads operator config from the repo's .env. AWS_PROFILE comes from
# the shell (so it doesn't leak into containerized services that share
# .env). State backend region defaults to AWS_REGION.

locals {
  env_file = "${get_repo_root()}/.env"

  aws_profile           = get_env("AWS_PROFILE", "")
  aws_region            = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${AWS_REGION:-us-east-1}\"")
  state_region          = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${HAIL_TERRAFORM_STATE_REGION:-$${AWS_REGION:-us-east-1}}\"")
  state_bucket          = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_TERRAFORM_STATE_BUCKET\"")
  lock_table            = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_TERRAFORM_LOCK_TABLE\"")
  name_prefix           = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_INBOUND_EMAIL_NAME_PREFIX\"")
  iam_user_name         = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_IAM_USER_NAME\"")
  hail_api_url          = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_API_URL\"")
  hail_inbound_secret   = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_INBOUND_HMAC_SECRET\"")
  hail_mail_base_domain = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_MAIL_BASE_DOMAIN\"")
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
  name_prefix              = local.name_prefix
  region                   = local.aws_region
  hail_api_url             = local.hail_api_url
  hail_inbound_hmac_secret = local.hail_inbound_secret
  hail_mail_base_domain    = local.hail_mail_base_domain
  aws_profile              = local.aws_profile
  iam_user_name            = local.iam_user_name
  lambda_source_dir        = "${get_repo_root()}/infra/ses-ingest-lambda"
}
