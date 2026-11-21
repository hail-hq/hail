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
COPY mcp/pyproject.toml /app/mcp/pyproject.toml
COPY api/pyproject.toml /app/api/pyproject.toml
COPY sdk/pyproject.toml /app/sdk/pyproject.toml
COPY voicebot/pyproject.toml /app/voicebot/pyproject.toml
RUN uv sync --frozen --package hailhq-mcp --no-install-workspace --no-dev

# ─── Stage 2: builder ─────────────────────────────────────────────
# Our own code, installed on top of the deps venv. Only these files go into
# the runtime image from here; they hold the hailhq namespace and its
# install records (importlib.metadata reads them).
FROM deps AS builder

COPY core /app/core
COPY mcp  /app/mcp
RUN uv sync --frozen --package hailhq-mcp --no-editable --no-dev && \
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
# shebangs (#!/app/.venv/bin/python) still resolve.
COPY --from=deps --chown=hail:hail /app/.venv /app/.venv
COPY --from=builder --chown=hail:hail /app/own/ /app/.venv/lib/python3.12/site-packages/

USER hail

# Check the installed runtime, including core's declared async SQLAlchemy support.
# No credentials or external services are available during this check.
RUN --network=none HAIL_API_KEY=hl_live_test python -c \
    "import hailhq.core.db; from hailhq.mcp.server import app"

EXPOSE 8081
# For stdio, override CMD with: python -u -m hailhq.mcp.stdio
CMD ["uvicorn", "hailhq.mcp.server:app", "--host", "0.0.0.0", "--port", "8081"]
