#!/usr/bin/env bash
# Take a still-running old VM out of service when you replace it
# (docs/operations/disaster-recovery.md).
#
# 1. Stop caddy, api and mcp now, so the old API's background jobs (webhooks,
#    forwards, carrier orders) stop before the new VM starts the same jobs.
# 2. Drain each voicebot: stop its text replies (marker file, see
#    textbot.DRAIN_FLAG), then stop it in the background. Live calls finish,
#    up to DRAIN_SECONDS, but their tools fail because the API is stopped.
#
# Keep the drain loop in step with scripts/deploy/roll-voicebot.sh.
#
# Usage, from your main checkout (the old VM's checkout may not have this file):
#   scp scripts/deploy/retire-vm.sh root@<OLD_IP>:/tmp/
#   ssh root@<OLD_IP> 'bash /tmp/retire-vm.sh'
set -euo pipefail

DRAIN_SECONDS="${DRAIN_SECONDS:-3660}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/hail}"
compose=(docker compose -f "$DEPLOY_PATH/docker-compose.yml" -f "$DEPLOY_PATH/docker-compose.prod.yml")

"${compose[@]}" stop caddy api mcp >&2

for cid in $("${compose[@]}" ps -q voicebot); do
  # A voicebot from an earlier deploy may still be draining. A second SIGTERM
  # makes LiveKit exit at once and drop its calls, so leave it alone.
  if pgrep -f "docker stop -t [0-9]+ $cid" >/dev/null; then
    echo "voicebot $cid is already draining" >&2
    continue
  fi
  echo "draining voicebot $cid (up to ${DRAIN_SECONDS}s)" >&2
  docker exec "$cid" touch /tmp/hail-draining >&2 || true
  setsid nohup sh -c "docker stop -t $DRAIN_SECONDS $cid && docker rm $cid" \
    >/dev/null 2>&1 </dev/null &
done
