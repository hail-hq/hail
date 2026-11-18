# syntax=docker/dockerfile:1.7

# ─── Stage 1: builder ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    HF_HOME=/app/.cache/huggingface \
    TORCH_HOME=/app/.cache/torch \
    XDG_CACHE_HOME=/app/.cache

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /usr/local/bin/

COPY core     /app/core
COPY voicebot /app/voicebot

# Retain the workspace manifests so --frozen installs exactly the reviewed lock.
# Only core and voicebot are installed; other workspace packages need metadata only.
COPY pyproject.toml uv.lock /app/
COPY api/pyproject.toml /app/api/pyproject.toml
COPY mcp/pyproject.toml /app/mcp/pyproject.toml
COPY sdk/pyproject.toml /app/sdk/pyproject.toml
RUN uv sync --frozen --package hailhq-voicebot --no-editable --no-dev

# Pre-download model weights (Silero VAD, turn-detector, etc.) so the runtime
# image is self-contained — the first call doesn't pay a multi-second download
# at the start of the prewarm step.
RUN /app/.venv/bin/python -m hailhq.voicebot.main download-files

# ─── Stage 2: runtime ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/.cache/huggingface \
    TORCH_HOME=/app/.cache/torch \
    XDG_CACHE_HOME=/app/.cache

RUN groupadd --system --gid 1000 hail && \
    useradd  --system --uid 1000 --gid hail --no-create-home hail

# Copy to the SAME path the builder used so the venv's entry-point script
# shebangs (#!/app/.venv/bin/python) still resolve.
COPY --from=builder --chown=hail:hail /app/.venv /app/.venv
# Pre-baked model weights from the builder.
COPY --from=builder --chown=hail:hail /app/.cache /app/.cache

USER hail

# Check the final runtime too, after copying dependencies out of the builder.
RUN --network=none python -c "import hailhq.voicebot.main"

CMD ["python", "-m", "hailhq.voicebot.main", "start"]
