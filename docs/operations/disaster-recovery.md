# Disaster recovery

The VM is stateless. Rebuild it from the repo, `.env`, and DNS. Data lives in the managed Postgres.

| Thing                  | Where it lives                       | If lost                                    |
| ---------------------- | ------------------------------------ | ------------------------------------------ |
| Code and images        | GitHub and ghcr.io                   | Nothing to restore. Deploy pulls them.     |
| `.env`                 | Main checkout on your machine        | Re-render from `.env.example`.             |
| Database               | Managed Postgres                     | Restore from the provider's backup.        |
| TLS certificates       | Caddy volume on the VM               | Caddy gets new ones on first request.      |
| Deploy key and host    | GitHub Actions secrets               | Make a new key and set the secrets.        |
| DNS                    | Registrar                            | Point `api.` and `mcp.` at the new VM.     |
| Carrier webhooks, SES  | Use the domain names, not the VM IP  | Follow DNS. No change.                     |

## Replace a lost or broken VM

1. Create an Ubuntu 22.04+ VM with a public IPv4. The cloud firewall may replace `ufw`.
2. Install Docker from the apt repository: <https://docs.docker.com/engine/install/ubuntu/>. The `docker` binary must be on the PATH of a non-interactive SSH shell, so do not use a snap package.
3. Clone the repo to the deploy path (`/opt/hail` by default).

   ```bash
   git clone https://github.com/hail-hq/hail /opt/hail
   ```

4. Copy `.env` from your main checkout to the VM. Never copy it from the old VM.

   ```bash
   scp .env root@<NEW_IP>:/opt/hail/.env
   ssh root@<NEW_IP> 'chmod 600 /opt/hail/.env'
   ```

5. Make a new CI-only deploy key and authorize it on the VM.

   ```bash
   ssh-keygen -t ed25519 -f hail-deploy -C "github-actions@hail" -N ""
   ssh root@<NEW_IP> "echo '$(cat hail-deploy.pub)' >> ~/.ssh/authorized_keys"
   ```

6. Point GitHub at the new VM, then deploy.

   ```bash
   gh secret set DEPLOY_SSH_PRIVATE_KEY < hail-deploy
   gh secret set DEPLOY_SSH_HOST --body <NEW_IP>
   gh secret set DEPLOY_SSH_USER --body root
   gh workflow run deploy.yml --ref main
   ```

7. Test the new VM before DNS moves. `-k` is needed because Caddy cannot get certificates until DNS points here.

   ```bash
   curl -k --resolve api.<domain>:443:<NEW_IP> https://api.<domain>/healthz
   curl -k --resolve mcp.<domain>:443:<NEW_IP> https://mcp.<domain>/healthz
   ```

   Both must return `{"status":"ok"}`.

8. At the registrar, change the A records `api` and `mcp` to `<NEW_IP>`. Check them with `dig +short api.<domain> @1.1.1.1`.
9. Wait for the old voicebot to finish live calls (up to about 1 hour). Then delete the old VM.

DNS caches can keep the old address for a while. Ask the registrar's name server (`dig +short mcp.<domain> @<registrar-ns>`) to see the real value.

## Lost database

1. Restore from the provider's backup or point-in-time recovery to a new database.
2. Set `DATABASE_URL` in `.env` to the new database. Copy `.env` to the VM.
3. Run the Deploy workflow. It runs `alembic upgrade head`, then restarts the services.
4. Check `/healthz`, one test call, and one test text.

Backups are the provider's job. Check that they are on, and test a restore, before you need one.

## After recovery

- Confirm metrics and traces arrive from the new containers (see [logfire.md](logfire.md)).
- Confirm one inbound call reaches the voicebot.
- Delete the temporary deploy key files from your machine.
