# VM deployment

Self-host Hail on a single Ubuntu VM behind HTTPS, with GitHub Actions deploying every commit on `main`. Database is **managed** (Neon, Supabase, RDS, whatever you like); the VM only runs the stateless services.

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

Voicebot is a worker — it dials out to LiveKit Cloud and needs no inbound port. Only `api.<domain>` and `mcp.<domain>` are public.

## Prerequisites

- An Ubuntu 22.04+ VM with a public IPv4 address. 2 vCPU / 4 GB RAM is enough to start.
- A managed Postgres reachable from the VM. Grab its connection string (typically `postgresql://USER:PASS@HOST/DB?sslmode=require`).
- A domain you control. You'll point `api.<domain>` and `mcp.<domain>` at the VM's IP.

## 1. Prepare the VM

SSH in as a sudo-capable user and run:

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

If your fork is private, clone via SSH (`git@github.com:<owner>/hail.git`) and authorise a deploy key on the VM — the CI deploy step runs `git fetch origin` on every push and will 403 otherwise.

> **UFW + Docker caveat:** Docker writes its own iptables rules that _bypass_ UFW. The compose file already binds api/mcp/minio ports to `127.0.0.1`, so external traffic can't reach them even though UFW would allow it locally. If you add any new published port, keep the `127.0.0.1:` prefix unless you intend it to be public.

## 2. DNS

Create two A records (or one wildcard) pointing at the VM:

| record         | type | value     |
| -------------- | ---- | --------- |
| `api.<domain>` | A    | _VM IPv4_ |
| `mcp.<domain>` | A    | _VM IPv4_ |

Verify before continuing — Caddy will fail to mint Let's Encrypt certs if either name doesn't resolve to the VM yet.

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

- All provider secrets (Twilio, LiveKit, Deepgram, Cartesia (+ optional ElevenLabs fallback), at least one LLM key — see `docs/setup/`).
- `HAIL_API_KEY` — generate with `openssl rand -base64 32 | tr -d '/+=' | head -c 40 | sed 's/^/hk_/'`.
- `DATABASE_URL` — the managed Postgres connection string. Most providers require `?sslmode=require`.
- `HAIL_DOMAIN` — your apex (e.g. `hail.example.com`). Both `api.` and `mcp.` subdomains derive from it.

`.env` is already gitignored — never commit it.

## 4. First boot

The compose invocations below are verbose because the prod overlay isn't the default — drop a shell alias into the deploy user's `~/.bashrc` if you'll be on the VM often:

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

If you'd rather skip the manual GHCR login, trigger the GitHub Actions workflow (step 6) first — its `docker login` step writes credentials the VM can reuse.

Seed your first phone number using the snippet in `docs/public/operations.md` → _Self-host: first-run setup_, swapping `docker compose exec postgres …` for `psql "$DATABASE_URL" -c …`.

Hit `https://api.<domain>/healthz` from your laptop — you should see `{"status":"ok"}`. Caddy mints a Let's Encrypt cert on first request; the first call after boot can take 10–20 seconds.

## 5. Configure GitHub Actions

Under **Settings → Secrets and variables → Actions**:

| name                     | type   | value                                                 |
| ------------------------ | ------ | ----------------------------------------------------- |
| `DEPLOY_SSH_HOST`        | secret | VM hostname or IP                                     |
| `DEPLOY_SSH_USER`        | secret | SSH user on the VM (in the `docker` group)            |
| `DEPLOY_SSH_PRIVATE_KEY` | secret | Private key matching an authorized public key         |
| `DEPLOY_SSH_PORT`        | secret | _(optional)_ SSH port; omit to default to 22          |
| `DEPLOY_PATH`            | _var_  | _(optional)_ repo checkout path; defaults `/opt/hail` |

Generate a deploy key dedicated to CI rather than reusing a personal one:

```bash
ssh-keygen -t ed25519 -f hail-deploy -C "github-actions@hail" -N ""
# On the VM: append hail-deploy.pub to ~/.ssh/authorized_keys
# In GitHub: paste hail-deploy (the private key) into DEPLOY_SSH_PRIVATE_KEY.
```

Optionally create a `production` GitHub Environment (Settings → Environments → New → `production`) with required reviewers if every deploy should gate on a manual approval — the deploy job targets `environment: production` and inherits any protection rules automatically.

GHCR push uses the workflow's `GITHUB_TOKEN` (already scoped `packages: write`); no extra PAT needed.

## 6. Trigger the first deploy

Push any commit to `main`, or run **Actions → Deploy → Run workflow** in the GitHub UI. The workflow:

1. Builds `api`, `voicebot`, `mcp` images in parallel and pushes both `sha-<commit>` and `latest` to ghcr.io/hail-hq/hail-\*.
2. SSHes the VM, fetches the new commit, runs `alembic upgrade head`, and `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` rolls the containers onto the freshly-pulled `:latest`.

`concurrency: deploy-prod` means simultaneous merges queue rather than race.

## Day-2

- **Tail logs**: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f api`
- **Restart one service**: `docker compose -f docker-compose.yml -f docker-compose.prod.yml restart api`
- **Roll back**: every build pushes a `sha-<commit>` tag. To pin an older build:

  ```bash
  docker pull ghcr.io/hail-hq/hail-api:sha-<previous-commit>
  docker tag  ghcr.io/hail-hq/hail-api:sha-<previous-commit> ghcr.io/hail-hq/hail-api:latest
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api
  ```

  Repeat per service. The cleanest rollback is still to revert the commit on `main` and let CI redeploy.

- **Update Caddy config**: edit `/opt/hail/Caddyfile`, then `docker compose -f docker-compose.yml -f docker-compose.prod.yml exec caddy caddy reload --config /etc/caddy/Caddyfile`.
- **Reclaim disk**: each deploy leaves the previous `sha-<commit>` image behind. Prune monthly with `docker image prune -f` (safe — only removes images with no container reference).

## Footguns

| symptom                                                      | fix                                                                                                                                                                                                |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Caddy logs `tls: no certificates configured` on first start  | DNS hasn't propagated yet, or :80/:443 isn't open. Confirm `dig api.<domain>` returns the VM IP and `sudo ufw status` shows both ports open.                                                       |
| `docker pull` 401 / "denied" from GHCR                       | The package is private by default. Set its visibility to public under GitHub → Packages → hail-\_ → Package settings, or rely on the workflow's `docker login` step.                               |
| `permission denied while trying to connect to docker daemon` | The deploy user must be in the `docker` group. Run `sudo usermod -aG docker $USER` on the VM and re-establish the SSH session.                                                                     |
| Caddy 502s for api.<domain>                                  | Caddy resolves `api` and `mcp` via Compose's internal DNS — all services share the `hail` project network. Don't split the project name across files.                                              |
| api/mcp port reachable from the public internet              | A published port without the `127.0.0.1:` prefix bypasses UFW. Check `docker compose -f docker-compose.yml -f docker-compose.prod.yml port api 8080` returns `127.0.0.1:8080`, not `0.0.0.0:8080`. |
