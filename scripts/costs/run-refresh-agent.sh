#!/usr/bin/env bash
# Runs Claude Code for costs-refresh.yml, locked down by --restricted and
# .github/prompts/costs-refresh-settings.json.
# Usage: run-refresh-agent.sh <prompt file> <budget usd> <minutes> <output json>
set -uo pipefail

# Bedrock only. Nothing in this run may call the Anthropic API.
unset ANTHROPIC_API_KEY
export CLAUDE_CODE_USE_BEDROCK=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

timeout -k 60s --signal=INT "${3}m" claude --restricted --safe-mode --strict-mcp-config -p "$(cat "$1")" \
  --model eu.anthropic.claude-sonnet-5-5 \
  --max-budget-usd "$2" \
  --tools 'Read,Grep,Glob,Edit,WebFetch' \
  --settings .github/prompts/costs-refresh-settings.json \
  --permission-mode dontAsk \
  --permission-prompts none \
  --output-format json < /dev/null > "$4"
