# syntax=docker/dockerfile:1.7

# ─── Stage 1: deps ────────────────────────────────────────────────
# Third-party packages only. This stage reads nothing but uv.lock and the
# workspace manifests, so a code change reuses its layer unchanged and the
# VM keeps one copy of it instead of one per deploy.
FROM python:3.12-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    HF_HOME=/app/.cache/huggingface \
    TORCH_HOME=/app/.cache/torch \
    XDG_CACHE_HOME=/app/.cache

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /usr/local/bin/

# Retain the workspace manifests so --frozen installs exactly the reviewed lock.
COPY pyproject.toml uv.lock /app/
COPY core/pyproject.toml /app/core/pyproject.toml
COPY voicebot/pyproject.toml /app/voicebot/pyproject.toml
COPY api/pyproject.toml /app/api/pyproject.toml
COPY mcp/pyproject.toml /app/mcp/pyproject.toml
COPY sdk/pyproject.toml /app/sdk/pyproject.toml
RUN uv sync --frozen --package hailhq-voicebot --no-install-workspace --no-dev

# Pre-download model weights (Silero VAD and the other installed plugins) so
# the runtime image is self-contained: the first call doesn't pay a
# multi-second download at the start of the prewarm step. LiveKit's own
# command finds every installed plugin, so it needs no Hail code.
RUN /app/.venv/bin/python -m livekit.agents download-files

# ─── Stage 2: builder ─────────────────────────────────────────────
# Our own code, installed on top of the deps venv. Only these files go into
# the runtime image from here; they hold the hailhq namespace and its
# install records (importlib.metadata reads them).
FROM deps AS builder

COPY core /app/core
COPY voicebot /app/voicebot
RUN uv sync --frozen --package hailhq-voicebot --no-editable --no-dev && \
    mkdir /app/own && \
    cp -a /app/.venv/lib/python3.12/site-packages/hailhq \
          /app/.venv/lib/python3.12/site-packages/hailhq_*.dist-info /app/own/

# ─── Stage 3: runtime ─────────────────────────────────────────────
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
COPY --from=deps --chown=hail:hail /app/.venv /app/.venv
# Pre-baked model weights.
COPY --from=deps --chown=hail:hail /app/.cache /app/.cache
COPY --from=builder --chown=hail:hail /app/own/ /app/.venv/lib/python3.12/site-packages/

USER hail

# Check the final runtime too, after copying dependencies out of the builder.
RUN --network=none python -c "import hailhq.voicebot.main"

CMD ["python", "-m", "hailhq.voicebot.main", "start"]
