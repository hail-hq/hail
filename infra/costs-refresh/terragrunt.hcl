# GitHub Actions access to Bedrock for .github/workflows/costs-refresh.yml.
# This stack is for the hail-hq/hail account only. Self-hosters do not need it,
# which is why it is not in infra/terraform. Reads the state settings from the
# repo's .env, like infra/terragrunt.hcl.

locals {
  env_file = "${get_repo_root()}/.env"

  aws_profile  = get_env("AWS_PROFILE", "")
  aws_region   = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${AWS_REGION:-us-east-1}\"")
  state_region = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$${HAIL_TERRAFORM_STATE_REGION:-$${AWS_REGION:-us-east-1}}\"")
  state_bucket = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_TERRAFORM_STATE_BUCKET\"")
  lock_table   = run_cmd("--terragrunt-quiet", "bash", "-c", "source ${local.env_file} && echo -n \"$HAIL_TERRAFORM_LOCK_TABLE\"")
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
    key            = "costs-refresh/terraform.tfstate"
    region         = local.state_region
    encrypt        = true
    dynamodb_table = local.lock_table
    profile        = local.aws_profile
  }
}

inputs = {
  region      = local.aws_region
  aws_profile = local.aws_profile
}
