# syntax=docker/dockerfile:1.7

# ─── Stage 1: deps ────────────────────────────────────────────────
# Third-party packages only. This stage reads nothing but uv.lock and the
# workspace manifests, so a code change reuses its layer unchanged and the
# VM keeps one copy of it instead of one per deploy.
FROM python:3.12-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /usr/local/bin/

# Retain the workspace manifests so --frozen installs exactly the reviewed lock.
COPY pyproject.toml uv.lock /app/
COPY core/pyproject.toml /app/core/pyproject.toml
COPY api/pyproject.toml /app/api/pyproject.toml
COPY mcp/pyproject.toml /app/mcp/pyproject.toml
COPY sdk/pyproject.toml /app/sdk/pyproject.toml
COPY voicebot/pyproject.toml /app/voicebot/pyproject.toml
RUN uv sync --frozen --package hailhq-api --no-install-workspace --no-dev

# ─── Stage 2: builder ─────────────────────────────────────────────
# Our own code, installed on top of the deps venv. Only these files go into
# the runtime image from here; they hold the hailhq namespace and its
# install records (importlib.metadata reads them).
FROM deps AS builder

COPY core /app/core
COPY api  /app/api
RUN uv sync --frozen --package hailhq-api --no-editable --no-dev && \
    mkdir /app/own && \
    cp -a /app/.venv/lib/python3.12/site-packages/hailhq \
          /app/.venv/lib/python3.12/site-packages/hailhq_*.dist-info /app/own/

# ─── Stage 3: runtime ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 1000 hail && \
    useradd  --system --uid 1000 --gid hail --no-create-home hail

# Copy to the SAME path the builder used so the venv's entry-point script
# shebangs (#!/app/.venv/bin/python) still resolve. Renaming to /opt/venv
# breaks them — see https://docs.astral.sh/uv/guides/integration/docker/.
COPY --from=deps --chown=hail:hail /app/.venv /app/.venv

# Alembic config + migrations are read from the filesystem (not the venv) at
# `alembic upgrade head` time. Without these, `docker compose run --rm api
# alembic upgrade head` fails with "No 'script_location' key found in
# configuration." The runtime image stays small — we only copy what alembic
# actually reads, not the rest of api/.
COPY --chown=hail:hail api/alembic.ini /app/api/alembic.ini
COPY --chown=hail:hail api/migrations  /app/api/migrations

# costs/<carrier>.json are read at runtime by hailhq.core.telephony_catalog
# (the acquire allow-list / price / capability source of truth).
COPY --chown=hail:hail costs /app/costs
ENV HAIL_TELEPHONY_CATALOG_DIR=/app/costs

# Our code last: it changes on almost every release, and the layers above
# stay cached when it does.
COPY --from=builder --chown=hail:hail /app/own/ /app/.venv/lib/python3.12/site-packages/
WORKDIR /app/api

USER hail

# Import the application with production dependencies before publishing the image.
RUN --network=none python -c "from hailhq.api.main import app"

EXPOSE 8080
CMD ["uvicorn", "hailhq.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
