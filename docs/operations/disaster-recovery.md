# Disaster recovery

The VM is stateless. Rebuild it from the repo, `.env`, and DNS. Data lives in Supabase Postgres.

| Thing                 | Where it lives                      | If lost                                   |
| --------------------- | ----------------------------------- | ----------------------------------------- |
| Code and images       | GitHub and ghcr.io                  | Nothing to restore. Deploy pulls them.    |
| `.env`                | Main checkout on your machine       | Copy it again from the main checkout.     |
| Database              | Supabase Postgres                   | Restore from a Supabase backup.           |
| TLS certificates      | Caddy volume on the VM              | Caddy gets new ones once DNS points here. |
| Deploy key and host   | GitHub Actions secrets              | Make a new key and set the secrets.       |
| DNS                   | Registrar                           | Point `api.`, `mcp.`, tracking at new VM. |
| Carrier webhooks, SES | Use the domain names, not the VM IP | Follow DNS. No change.                    |

## Replace a lost or broken VM

1. Prepare the new VM with [vm-deploy.md step 1](../public/self-host/vm-deploy.md#1-prepare-the-vm): Docker, firewall (22, 80, 443), and the repo in `/opt/hail`.
2. Copy `.env` from your main checkout. Skip vm-deploy.md step 3: `.env.example` has no secrets. Never copy `.env` from the old VM.

   ```bash
   scp .env root@<NEW_IP>:/opt/hail/.env
   ssh root@<NEW_IP> 'chmod 600 /opt/hail/.env'
   ```

   The deploy edits `.env` as `DEPLOY_SSH_USER`. If that user is not root, give it the file.

   ```bash
   ssh root@<NEW_IP> 'chown <DEPLOY_SSH_USER>: /opt/hail/.env'
   ```

3. Make a new deploy key and set the GitHub secrets with [vm-deploy.md step 5](../public/self-host/vm-deploy.md#5-configure-github-actions). Set `DEPLOY_SSH_HOST` to `<NEW_IP>`.
4. Deploy.

   ```bash
   gh workflow run deploy.yml --ref main
   ```

5. Test the new VM before DNS moves. Caddy has no certificate yet, so test the services on the VM's loopback ports.

   ```bash
   ssh root@<NEW_IP> 'curl -fsS http://127.0.0.1:8080/healthz'
   ssh root@<NEW_IP> 'curl -fsS http://127.0.0.1:8081/healthz'
   ```

   Both must return `{"status":"ok"}`.

6. At the registrar, change the A records `api`, `mcp`, and the `HAIL_TRACKING_DOMAIN` host (`.env`) to `<NEW_IP>`. Ask the registrar's name server, not a cache. Each must print `<NEW_IP>`.

   ```bash
   dig +short api.<domain> @<registrar-ns>
   dig +short mcp.<domain> @<registrar-ns>
   dig +short <HAIL_TRACKING_DOMAIN> @<registrar-ns>
   ```

7. Restart Caddy so it asks for certificates now, not after its retry wait. Then check the logs for `certificate obtained successfully`.

   ```bash
   ssh root@<NEW_IP> 'cd /opt/hail && docker compose \
     -f docker-compose.yml -f docker-compose.prod.yml restart caddy'
   ssh root@<NEW_IP> 'cd /opt/hail && docker compose \
     -f docker-compose.yml -f docker-compose.prod.yml logs caddy \
     | grep -i certificate'
   ```

8. Add shell shortcuts on the new VM (next section).
9. If the old VM still runs, stop it. Until then its voicebot keeps taking calls and texts, and its API runs the same background jobs as the new one. This drains the old voicebot (live calls finish, up to about 1 hour). The old API, MCP and Caddy stop after it: the voicebot's tools call the local API, and clients with a cached DNS answer still reach it.

   ```bash
   ssh root@<OLD_IP> '
   cd /opt/hail
   C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
   for cid in $($C ps -q voicebot)
   do
   pgrep -f "docker stop -t [0-9]+ $cid" >/dev/null && continue
   docker exec "$cid" touch /tmp/hail-draining
   setsid nohup docker stop -t 3660 "$cid" >/dev/null 2>&1 </dev/null &
   done
   setsid nohup sh -c "
   while [ -n \"\$($C ps -q voicebot)\" ]
   do
   sleep 30
   done
   $C stop caddy api mcp
   " >/dev/null 2>&1 </dev/null &
   '
   ```

   The `pgrep` line skips a voicebot that an earlier deploy is already draining. A second stop would drop its calls. Delete the old VM when `docker ps` there shows no containers.

### Shell shortcuts on the VM

```bash
ssh root@<NEW_IP> '
ln -sfn /opt/hail ~/hail
add() { grep -qxF "$1" ~/.bashrc || echo "$1" >> ~/.bashrc; }
add "alias comp=\"docker compose -f docker-compose.yml -f docker-compose.prod.yml\""
add "alias gpp=\"git pull --prune\""
'
```

After the next login, `cd ~/hail` opens the repo. Then `comp ps` and `comp logs -f api` work. The `comp` alias uses relative paths, so run it inside the repo.

## Lost database (Supabase)

The database is a Supabase project, not on the VM. Source: <https://supabase.com/docs/guides/platform/backups>.

1. Choose where to restore. In place overwrites the current data. To keep it, restore into a new project instead ([Duplicate Project](https://supabase.com/docs/guides/platform/clone-project)), then put the new connection string in `DATABASE_URL` in `.env` and copy `.env` to the VM.
2. In place: open [Database > Backups](https://supabase.com/dashboard/project/_/database/backups/scheduled). With daily backups: pick the newest backup before the problem and confirm the restore. With point-in-time recovery: open **Point in Time**, click **Start a restore**, pick a time, and confirm.
3. The project is unreachable during the restore. The API returns errors until it ends.
4. Backups do not store custom role passwords. If `DATABASE_URL` uses a custom role, reset its password, update `.env`, and copy it to the VM.
5. Run the Deploy workflow. It runs `alembic upgrade head`, then restarts the services.
6. `/healthz` does not touch the database. Check that the deploy log shows `=== current schema revision ===` followed by a revision, then place one test call and send one test text.

Daily backups and point-in-time recovery need a paid Supabase plan. Check that backups are on, and test a restore, before you need one.

## After recovery

- Confirm metrics and traces arrive from the new containers (see [logfire.md](logfire.md)).
- Confirm one inbound call reaches the voicebot.
- Delete the temporary deploy key files from your machine.
