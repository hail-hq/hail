# syntax=docker/dockerfile:1.7

# ─── Stage 1: builder ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

COPY core /app/core
COPY mcp  /app/mcp

# Minimal workspace root so `[tool.uv.sources] hailhq-core = { workspace = true }`
# in mcp/pyproject.toml resolves without the repo-root pyproject in the build
# context. Stays in the builder layer; doesn't reach runtime.
RUN printf '[tool.uv.workspace]\nmembers = ["core", "mcp"]\n' > /app/pyproject.toml && \
    uv sync --package hailhq-mcp --no-editable --no-dev

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
CMD ["uvicorn", "hailhq.mcp.server:app", "--host", "0.0.0.0", "--port", "8081"]
