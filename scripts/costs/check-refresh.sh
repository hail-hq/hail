#!/usr/bin/env bash
# The checks costs-refresh.yml runs on the agent's edits before a PR opens.
# Runs every check and reports all failures, because the repair pass gets one
# chance to see them.
# Usage: check-refresh.sh
set -uo pipefail

failed=0
run() {
  "$@" || failed=1
}

run pnpm exec prettier --write costs/llm.json costs/stt.json costs/tts.json costs/sms.json
for name in llm stt tts sms; do
  run pipx run check-jsonschema==0.29.4 --schemafile "costs/schema/$name.schema.json" "costs/$name.json"
done
run pnpm costs:featured
run node --test scripts/costs/*.test.mjs
for f in costs/llm.json costs/stt.json costs/tts.json; do
  dupes=$(jq -c '[.models[] | .aliases // [] | .[]] | group_by(.) | map(select(length > 1) | .[0])' "$f")
  unresolved=$(jq -c '(.models | map(.model_id)) as $ids | [.models[] | select(.replaced_by_model_id != null) | select((.replaced_by_model_id as $r | $ids | index($r)) == null) | .model_id]' "$f")
  if [ "$dupes" != "[]" ] || [ "$unresolved" != "[]" ]; then
    echo "::error file=$f::aliases: $dupes ; replaced_by: $unresolved"
    failed=1
  fi
done
run pnpm site:build

exit "$failed"
