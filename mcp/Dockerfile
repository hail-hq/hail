# syntax=docker/dockerfile:1.7

# ─── Stage 1: builder ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /usr/local/bin/

COPY core /app/core
COPY mcp  /app/mcp

# Retain the workspace manifests so --frozen installs exactly the reviewed lock.
# Only core and mcp are installed; other workspace packages need metadata only.
COPY pyproject.toml uv.lock /app/
COPY api/pyproject.toml /app/api/pyproject.toml
COPY sdk/pyproject.toml /app/sdk/pyproject.toml
COPY voicebot/pyproject.toml /app/voicebot/pyproject.toml
RUN uv sync --frozen --package hailhq-mcp --no-editable --no-dev

# ─── Stage 2: runtime ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 1000 hail && \
    useradd  --system --uid 1000 --gid hail --no-create-home hail

# Copy to the SAME path the builder used so the venv's entry-point script
# shebangs (#!/app/.venv/bin/python) still resolve.
COPY --from=builder --chown=hail:hail /app/.venv /app/.venv

USER hail

EXPOSE 8081
# For stdio, override CMD with: python -u -m hailhq.mcp.stdio
CMD ["uvicorn", "hailhq.mcp.server:app", "--host", "0.0.0.0", "--port", "8081"]
