# Disaster recovery

The VM is stateless. Rebuild it from the repo, `.env`, and DNS. Data lives in Supabase Postgres.

| Thing                 | Where it lives                      | If lost                                   |
| --------------------- | ----------------------------------- | ----------------------------------------- |
| Code and images       | GitHub and ghcr.io                  | Nothing to restore. Deploy pulls them.    |
| `.env`                | Main checkout on your machine       | Copy it again from the main checkout.     |
| Database              | Supabase Postgres                   | Restore from a Supabase backup.           |
| TLS certificates      | Caddy volume on the VM              | Caddy gets new ones once DNS points here. |
| Deploy key and host   | GitHub Actions secrets              | Make a new key and set the secrets.       |
| DNS                   | DNS host (`dig +short NS <domain>`) | Point `api.`, `mcp.`, tracking at new VM. |
| Carrier webhooks, SES | Use the domain names, not the VM IP | Follow DNS. No change.                    |

## Replace a lost or broken VM

Run these from your main checkout.

1. Take the old VM out of service first, so it never runs jobs next to the new one.
   - **Old VM is down or SSH fails:** delete it (or power it off) in the cloud provider's console. Its services use `restart: unless-stopped`, so they start again if the VM comes back.
   - **Old VM still runs:** run [`retire-vm.sh`](../../scripts/deploy/retire-vm.sh). It stops Caddy, the API and MCP, and drains the voicebot. Live calls finish (up to about 1 hour) without tools.

     ```bash
     scp scripts/deploy/retire-vm.sh root@<OLD_IP>:/tmp/
     ssh root@<OLD_IP> 'bash /tmp/retire-vm.sh'
     ```

   The API, MCP, texts and new calls are down from here until step 8.

2. Prepare the new VM with [vm-deploy.md step 1](../public/self-host/vm-deploy.md#1-prepare-the-vm): Docker, firewall (22, 80, 443), and the repo in `/opt/hail`. The disk Docker uses needs at least 20 GiB free, or the deploy stops.
3. Copy `.env` from your main checkout. Skip vm-deploy.md step 3: `.env.example` has no secrets. Never copy `.env` from the old VM.

   ```bash
   scp .env root@<NEW_IP>:/opt/hail/.env
   ssh root@<NEW_IP> 'chmod 600 /opt/hail/.env'
   ```

   The deploy edits `.env` as `DEPLOY_SSH_USER`. If that user is not root, give it the file.

   ```bash
   ssh root@<NEW_IP> 'chown <DEPLOY_SSH_USER>: /opt/hail/.env'
   ```

4. Make a new deploy key and set the GitHub secrets with [vm-deploy.md step 5](../public/self-host/vm-deploy.md#5-configure-github-actions). Set `DEPLOY_SSH_HOST` to `<NEW_IP>`.
5. Deploy. Wait for the run to finish on the [Deploy workflow page](https://github.com/hail-hq/hail/actions/workflows/deploy.yml). Then stop Caddy: before DNS moves, each certificate request fails, and Let's Encrypt blocks a host name after 5 failures in an hour.

   ```bash
   gh workflow run deploy.yml --ref main
   ssh root@<NEW_IP> 'cd /opt/hail && docker compose \
     -f docker-compose.yml -f docker-compose.prod.yml stop caddy'
   ```

6. Test the new VM on its loopback ports. Both must return `{"status":"ok"}`.

   ```bash
   ssh root@<NEW_IP> 'curl -fsS http://127.0.0.1:8080/healthz'
   ssh root@<NEW_IP> 'curl -fsS http://127.0.0.1:8081/healthz'
   ```

7. Find your DNS host: the first command prints its name servers. There, change the A records `api`, `mcp`, and the `HAIL_TRACKING_DOMAIN` host (`.env`) to `<NEW_IP>`. Skip the tracking host if `HAIL_TRACKING_DOMAIN` is empty. Each check must print `<NEW_IP>`.

   ```bash
   dig +short NS <domain>
   dig +short api.<domain> @<name-server>
   dig +short mcp.<domain> @<name-server>
   dig +short <HAIL_TRACKING_DOMAIN> @<name-server>
   ```

8. Start Caddy. Then check the logs for `certificate obtained successfully`.

   ```bash
   ssh root@<NEW_IP> 'cd /opt/hail && docker compose \
     -f docker-compose.yml -f docker-compose.prod.yml start caddy'
   ssh root@<NEW_IP> 'cd /opt/hail && docker compose \
     -f docker-compose.yml -f docker-compose.prod.yml logs caddy \
     | grep -i certificate'
   ```

9. Add shell shortcuts on the new VM (next section).
10. Delete the old VM once `docker ps` there shows no containers.

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

1. Stop the API, MCP and voicebot, so no job acts on old rows after the restore.

   ```bash
   ssh root@<VM_IP> 'cd /opt/hail && docker compose \
     -f docker-compose.yml -f docker-compose.prod.yml stop api mcp voicebot'
   ```

2. Choose where to restore. In place overwrites the current data. To keep it, restore into a new project instead ([Duplicate Project](https://supabase.com/docs/guides/platform/clone-project)), then put the new connection string in `DATABASE_URL` in `.env` and copy `.env` to the VM.
3. In place: open [Database > Backups](https://supabase.com/dashboard/project/_/database/backups/scheduled). With daily backups: pick the newest backup before the problem and confirm the restore. With point-in-time recovery: open **Point in Time**, click **Start a restore**, pick a time, and confirm.
4. Backups do not store custom role passwords. If `DATABASE_URL` uses a custom role, reset its password, update `.env`, and copy it to the VM.
5. Check what changed between the backup time and now. The database lost it, but the carrier and payment records still have it.
   - `phone_numbers`: compare with the numbers in each carrier's console. Add or release the ones bought or released in that time.
   - `carrier_verifications`: compare each pending row with its status at the carrier.
   - `account_credits`: compare with the top-ups and charges in the payment records.
6. Run the Deploy workflow. It runs `alembic upgrade head`, then starts the services again.
7. `/healthz` does not touch the database. Check that the deploy log shows `=== current schema revision ===` followed by a revision, then place one test call and send one test text.

Daily backups and point-in-time recovery need a paid Supabase plan. Check that backups are on, and test a restore, before you need one.

## After recovery

- Confirm metrics and traces arrive from the new containers (see [logfire.md](logfire.md)).
- Confirm one inbound call reaches the voicebot.
- Delete the temporary deploy key files from your machine.
