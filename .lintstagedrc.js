module.exports = {
  // Python — lint with Ruff (auto-fix), format with Black. Versions
  // pinned in lockstep with .github/workflows/ci.yml: unpinned uvx pulls
  // the latest release, and a linter upgrade mid-PR rewrites staged files
  // to a style CI then rejects.
  "**/*.py": ["uvx ruff@0.15.12 check --fix", "uvx black@26.5.1"],

  // Go — format.
  "cli/**/*.go": ["gofmt -w"],

  // Markdown / JSON / YAML — format with Prettier.
  "**/*.{md,json,yml,yaml}": ["prettier --write"],

  // Terraform / Terragrunt — format the whole infra dir in one shot.
  // Per-file invocation is fragile: lint-staged passes absolute file
  // paths, and `terraform fmt` resolves them relative to its own cwd
  // (the repo root under husky) which doesn't always agree with what
  // git stage rewrote. `-chdir=...` pins the directory either way.
  "infra/**/*.tf": () => "terraform -chdir=infra/terraform fmt",
  "infra/**/*.hcl": () => "bash -c 'cd infra && terragrunt hcl format'",

  // Dockerfiles — no formatter in v1; add hadolint via CI later.
};
