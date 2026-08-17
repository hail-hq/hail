# VM deployment

Self-host Hail on one Ubuntu VM behind HTTPS. GitHub Actions deploys every commit on `main`. The database is **managed** (Neon, Supabase, RDS, or your choice). The VM runs only the stateless services.

```
        ┌───────────────────────────────────────────────────────┐
        │  GitHub Actions  ──build──▶  ghcr.io/hail-hq/hail-*   │
        │       │                                               │
        │       └──ssh──▶  VM: docker compose pull && up -d     │
        └───────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Ubuntu VM                                                   │
   │                                                              │
   │   :80 / :443  ──▶  Caddy  ──▶  api  (api.<domain>)           │
   │                            └─▶  mcp  (mcp.<domain>)          │
   │                                                              │
   │                   voicebot ──outbound──▶ LiveKit Cloud       │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  └──▶ managed Postgres (Neon / RDS / …)
```

The voicebot is a worker. It dials out to LiveKit Cloud and needs no inbound port. Only `api.<domain>` and `mcp.<domain>` are public.

## Prerequisites

- An Ubuntu 22.04+ VM with a public IPv4 address. 2 vCPU / 4 GB RAM is sufficient to start.
- A managed Postgres that the VM can reach. Copy its connection string (typically `postgresql://USER:PASS@HOST/DB?sslmode=require`).
- A domain that you control. You point `api.<domain>` and `mcp.<domain>` at the VM's IP.

## 1. Prepare the VM

Connect with SSH as a sudo-capable user. Then run:

```bash
# Docker engine + compose plugin.
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
# Re-login or `newgrp docker` so the group change takes effect.

# Firewall: only 22, 80, 443 reach the host.
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp   # HTTP/3
sudo ufw enable

# Working directory.
sudo mkdir -p /opt/hail
sudo chown "$USER:$USER" /opt/hail
git clone https://github.com/hail-hq/hail /opt/hail
cd /opt/hail
```

If your fork is private, clone via SSH (`git@github.com:<owner>/hail.git`) and authorize a deploy key on the VM. The CI deploy step runs `git fetch origin` on every push and gets a 403 error otherwise.

> **UFW + Docker caveat:** Docker writes its own iptables rules that _bypass_ UFW. The compose file binds the api/mcp/minio ports to `127.0.0.1`. Thus external traffic cannot reach them, even if UFW permits it locally. If you add a new published port, keep the `127.0.0.1:` prefix, unless you intend the port to be public.

## 2. DNS

Create two A records (or one wildcard) that point at the VM:

| record         | type | value     |
| -------------- | ---- | --------- |
| `api.<domain>` | A    | _VM IPv4_ |
| `mcp.<domain>` | A    | _VM IPv4_ |

If either name does not resolve to the VM yet, Caddy cannot get Let's Encrypt certs. Verify the records before you continue.

```bash
dig +short api.<domain>
dig +short mcp.<domain>
```

## 3. Configure `.env` on the VM

```bash
cd /opt/hail
cp .env.example .env
```

Edit `/opt/hail/.env` and fill in:

- All provider secrets (Twilio, LiveKit, Deepgram, Cartesia, optional ElevenLabs fallback, and at least one LLM key); see [Twilio](./twilio.md) and [LiveKit Cloud](./livekit-cloud.md).
- `HAIL_API_KEY` — generate with `openssl rand -base64 32 | tr -d '/+=' | head -c 40 | sed 's/^/hk_/'`.
- `DATABASE_URL` — the managed Postgres connection string. Most providers require `?sslmode=require`.
- `HAIL_DOMAIN` — your apex (for example, `hail.example.com`). Both `api.` and `mcp.` subdomains derive from it.

The `.env` file is already gitignored. Never commit it.

## 4. First boot

The compose invocations below are verbose because the prod overlay is not the default. If you use the VM often, add a shell alias to the deploy user's `~/.bashrc`:

```bash
echo "alias dcprod='docker compose -f /opt/hail/docker-compose.yml -f /opt/hail/docker-compose.prod.yml'" >> ~/.bashrc
```

```bash
cd /opt/hail
# Log in to GHCR so docker can pull (one-time; the CI deploy will re-login
# each run). Create a Personal Access Token with `read:packages`.
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

To skip the manual GHCR login, trigger the GitHub Actions workflow (step 6) first. Its `docker login` step writes credentials that the VM can use again.

Bind your first phone number using [Self-host: first-run setup](./operations.md#self-host-first-run-setup). Because this deployment uses managed Postgres, replace `docker compose exec postgres …` with `psql "$DATABASE_URL" -c …`.

Send a request to `https://api.<domain>/healthz` from your laptop. You must see `{"status":"ok"}`. Caddy gets a Let's Encrypt cert on the first request. The first call after boot can take 10–20 seconds.

## 5. Configure GitHub Actions

Set these under **Settings → Secrets and variables → Actions**:

| name                     | type   | value                                                 |
| ------------------------ | ------ | ----------------------------------------------------- |
| `DEPLOY_SSH_HOST`        | secret | VM hostname or IP                                     |
| `DEPLOY_SSH_USER`        | secret | SSH user on the VM (in the `docker` group)            |
| `DEPLOY_SSH_PRIVATE_KEY` | secret | Private key matching an authorized public key         |
| `DEPLOY_SSH_PORT`        | secret | _(optional)_ SSH port; omit to default to 22          |
| `DEPLOY_PATH`            | _var_  | _(optional)_ repo checkout path; defaults `/opt/hail` |

Generate a deploy key dedicated to CI. Do not use a personal key again:

```bash
ssh-keygen -t ed25519 -f hail-deploy -C "github-actions@hail" -N ""
# On the VM: append hail-deploy.pub to ~/.ssh/authorized_keys
# In GitHub: paste hail-deploy (the private key) into DEPLOY_SSH_PRIVATE_KEY.
```

If every deploy must gate on a manual approval, create a `production` GitHub Environment (Settings → Environments → New → `production`) with required reviewers. The deploy job targets `environment: production` and inherits any protection rules automatically.

The GHCR push uses the workflow's `GITHUB_TOKEN` (already scoped `packages: write`). No extra PAT is necessary.

## 6. Trigger the first deploy

Push any commit to `main`, or run **Actions → Deploy → Run workflow** in the GitHub UI. The workflow:

1. Builds `api`, `voicebot`, `mcp` images in parallel and pushes both `sha-<commit>` and `latest` to ghcr.io/hail-hq/hail-\*.
2. Connects to the VM with SSH, fetches the new commit, and runs `alembic upgrade head`. Then `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` rolls the containers onto the newly pulled `:latest`.

`concurrency: deploy-prod` makes simultaneous merges queue. They do not race.

## Day-2

- **Tail logs**: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f api`
- **Restart one service**: `docker compose -f docker-compose.yml -f docker-compose.prod.yml restart api`
- **Roll back**: every build pushes a `sha-<commit>` tag. To pin an older build:

  ```bash
  docker pull ghcr.io/hail-hq/hail-api:sha-<previous-commit>
  docker tag  ghcr.io/hail-hq/hail-api:sha-<previous-commit> ghcr.io/hail-hq/hail-api:latest
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api
  ```

  Repeat for each service. The cleanest rollback is to revert the commit on `main` and let CI deploy again.

- **Update Caddy config**: edit `/opt/hail/Caddyfile`, then `docker compose -f docker-compose.yml -f docker-compose.prod.yml exec caddy caddy reload --config /etc/caddy/Caddyfile`.
- **Reclaim disk**: each deploy leaves the previous `sha-<commit>` image behind. Prune monthly with `docker image prune -f`. This is safe; it removes only images with no container reference.

## Footguns

| symptom                                                      | fix                                                                                                                                                                                                |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Caddy logs `tls: no certificates configured` on first start  | DNS has not propagated yet, or :80/:443 is not open. Confirm that `dig api.<domain>` returns the VM IP and `sudo ufw status` shows both ports open.                                                |
| `docker pull` 401 / "denied" from GHCR                       | The package is private by default. Set its visibility to public under GitHub → Packages → hail-\_ → Package settings, or rely on the workflow's `docker login` step.                               |
| `permission denied while trying to connect to docker daemon` | The deploy user must be in the `docker` group. Run `sudo usermod -aG docker $USER` on the VM and re-establish the SSH session.                                                                     |
| Caddy 502s for api.<domain>                                  | Caddy resolves `api` and `mcp` via Compose's internal DNS — all services share the `hail` project network. Do not split the project name across files.                                             |
| api/mcp port reachable from the public internet              | A published port without the `127.0.0.1:` prefix bypasses UFW. Check `docker compose -f docker-compose.yml -f docker-compose.prod.yml port api 8080` returns `127.0.0.1:8080`, not `0.0.0.0:8080`. |
