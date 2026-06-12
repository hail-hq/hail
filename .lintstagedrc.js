module.exports = {
  // Python — lint with Ruff (auto-fix), format with Black.
  "**/*.py": ["uvx ruff check --fix", "uvx black"],

  // Go — format.
  "cli/**/*.go": ["gofmt -w"],

  // Markdown / JSON / YAML — format with Prettier.
  "**/*.{md,json,yml,yaml}": ["prettier --write"],

  // Terraform — canonical formatter, in-place.
  "infra/**/*.tf": ["terraform fmt"],

  // Terragrunt — canonical formatter for .hcl files (also matches the
  // `terragrunt hcl format --check` gate run in CI).
  "infra/**/*.hcl": ["terragrunt hcl format"],

  // Dockerfiles — no formatter in v1; add hadolint via CI later.
};
