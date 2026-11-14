#!/usr/bin/env bash
# Start PgBouncer in transaction mode on 127.0.0.1:6432, in front of the
# Postgres on 127.0.0.1:5432 (user, password and database "hail").
# max_prepared_statements = 0 keeps it from tracking prepared statements,
# like a hosted shared pooler, so a driver that names statements fails.
#
# Usage: scripts/ci/start-transaction-pooler.sh [container-name]
set -euo pipefail

name="${1:-hail-transaction-pooler}"
config="$(mktemp -d)"
cat > "$config/pgbouncer.ini" <<'INI'
[databases]
hail = host=127.0.0.1 port=5432 dbname=hail

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
default_pool_size = 3
max_client_conn = 200
max_prepared_statements = 0
INI
echo '"hail" "hail"' > "$config/userlist.txt"
chmod 644 "$config"/*

docker run -d --name "$name" --network host \
  -v "$config:/etc/pgbouncer:ro" edoburu/pgbouncer:latest >/dev/null
for _ in $(seq 30); do
  if pg_isready -q -h 127.0.0.1 -p 6432; then
    exit 0
  fi
  sleep 1
done
docker logs "$name"
exit 1
