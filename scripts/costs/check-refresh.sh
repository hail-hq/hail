#!/usr/bin/env bash
# The checks costs-refresh.yml runs on the agent's edits before a PR opens.
# Usage: check-refresh.sh
set -euo pipefail

pnpm exec prettier --write costs/llm.json costs/stt.json costs/tts.json costs/sms.json
pnpm costs:validate
pnpm costs:featured
node --test scripts/costs/*.test.mjs
for f in costs/llm.json costs/stt.json costs/tts.json; do
  dupes=$(jq -c '[.models[] | .aliases // [] | .[]] | group_by(.) | map(select(length > 1) | .[0])' "$f")
  unresolved=$(jq -c '(.models | map(.model_id)) as $ids | [.models[] | select(.replaced_by_model_id != null) | select((.replaced_by_model_id as $r | $ids | index($r)) == null) | .model_id]' "$f")
  if [ "$dupes" != "[]" ] || [ "$unresolved" != "[]" ]; then
    echo "$f aliases: $dupes ; replaced_by: $unresolved"
    exit 1
  fi
done
pnpm site:build
