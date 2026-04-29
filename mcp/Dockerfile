# syntax=docker/dockerfile:1.7

# ─── Stage 1: builder ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

COPY core /build/core
COPY mcp  /build/mcp

# Minimal workspace root so `[tool.uv.sources] hailhq-core = { workspace = true }`
# in mcp/pyproject.toml resolves without the repo-root pyproject in the build
# context. Stays in the builder layer; doesn't reach runtime.
RUN printf '[tool.uv.workspace]\nmembers = ["core", "mcp"]\n' > /build/pyproject.toml && \
    uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python /build/core && \
    uv pip install --python /opt/venv/bin/python /build/mcp

# ─── Stage 2: runtime ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 1000 hail && \
    useradd  --system --uid 1000 --gid hail --no-create-home hail

COPY --from=builder --chown=hail:hail /opt/venv /opt/venv

USER hail

EXPOSE 8081
CMD ["uvicorn", "hailhq.mcp.server:app", "--host", "0.0.0.0", "--port", "8081"]
