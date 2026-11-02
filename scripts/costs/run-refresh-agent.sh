#!/usr/bin/env bash
# Runs Claude Code for costs-refresh.yml, locked down by --restricted and
# .github/prompts/costs-refresh-settings.json.
# Usage: run-refresh-agent.sh <prompt file> <budget usd> <minutes> <output json>
set -uo pipefail

timeout -k 60s --signal=INT "${3}m" claude --restricted --safe-mode --strict-mcp-config -p "$(cat "$1")" \
  --model claude-sonnet-5-5 \
  --max-budget-usd "$2" \
  --tools 'Read,Grep,Glob,Edit,WebFetch,WebSearch' \
  --settings .github/prompts/costs-refresh-settings.json \
  --permission-mode dontAsk \
  --permission-prompts none \
  --output-format json < /dev/null > "$4"
