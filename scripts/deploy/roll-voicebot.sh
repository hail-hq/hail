#!/usr/bin/env bash
# Swap the voicebot for a new image without dropping live calls.
#
# 1. Start one voicebot on the pulled image next to the running ones.
# 2. Wait until it has registered with LiveKit. New calls go to it from then on.
# 3. Stop each old voicebot's text replies (marker file, see textbot.DRAIN_FLAG),
#    then stop the voicebot in the background. SIGTERM makes the LiveKit worker
#    drain: it takes no new calls and exits once its live calls end. Docker
#    kills it after DRAIN_SECONDS: LiveKit's own drain limit (1 hour) plus a
#    minute for the calls to close cleanly.
#
# If the new voicebot does not register, it is removed and the old ones keep
# running, so a bad image never leaves the number without an agent.
#
# Usage: scripts/deploy/roll-voicebot.sh docker compose -f a.yml -f b.yml
# Prints the new container id on stdout; progress goes to stderr.
set -euo pipefail

DRAIN_SECONDS="${DRAIN_SECONDS:-3660}"
READY_SECONDS="${READY_SECONDS:-120}"
compose=("$@")

# A stopped voicebot left behind would be started again by --scale, on its old
# image, and taken for the new one. Remove it first.
"${compose[@]}" rm -f voicebot >&2

old="$("${compose[@]}" ps -q voicebot)"
count="$(printf '%s\n' "$old" | grep -c . || true)"

"${compose[@]}" up -d --no-deps --no-recreate --scale "voicebot=$((count + 1))" voicebot >&2

new=""
for cid in $("${compose[@]}" ps -q voicebot); do
  printf '%s\n' "$old" | grep -qx "$cid" || new="$cid"
done
if [ -z "$new" ]; then
  echo "::error::no new voicebot container was started" >&2
  exit 1
fi

ready=""
for _ in $(seq "$READY_SECONDS"); do
  # Not grep -q: it exits at the first match, docker logs then gets SIGPIPE,
  # and pipefail turns a match into a failure.
  if docker logs "$new" 2>&1 | grep "registered worker" >/dev/null; then
    ready=1
    break
  fi
  [ "$(docker inspect -f '{{.State.Running}}' "$new")" = true ] || break
  sleep 1
done
if [ -z "$ready" ]; then
  echo "::error::the new voicebot did not register with LiveKit; the old one keeps running" >&2
  docker logs --tail 50 "$new" >&2 || true
  docker rm -f "$new" >&2
  exit 1
fi
echo "voicebot $new registered with LiveKit" >&2

for cid in $old; do
  # A voicebot from an earlier deploy may still be draining. A second SIGTERM
  # makes LiveKit exit at once and drop its calls, so leave it alone.
  if pgrep -f "docker stop -t [0-9]+ $cid" >/dev/null; then
    echo "voicebot $cid is already draining" >&2
    continue
  fi
  echo "draining voicebot $cid (up to ${DRAIN_SECONDS}s)" >&2
  # Its text worker stops on this file, so texts move to the new code now.
  docker exec "$cid" touch /tmp/hail-draining >&2 || true
  setsid nohup sh -c "docker stop -t $DRAIN_SECONDS $cid && docker rm $cid" \
    >/dev/null 2>&1 </dev/null &
done

echo "$new"
