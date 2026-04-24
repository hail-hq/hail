module.exports = {
  // Python — lint with Ruff (auto-fix), format with Black.
  "**/*.py": ["uvx ruff check --fix", "uvx black"],

  // Go — format.
  "cli/**/*.go": ["gofmt -w"],

  // Markdown / JSON / YAML — format with Prettier.
  "**/*.{md,json,yml,yaml}": ["prettier --write"],

  // Dockerfiles — no formatter in v1; add hadolint via CI later.
};
