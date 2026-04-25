# v1 Outbound Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Hail v0.1.0 — an AI agent can call `POST /calls`, have Hail dial a phone number via Twilio, run a voice pipeline through LiveKit Cloud, speak with the callee using pluggable LLM/STT/TTS, and return the transcript + recording.

**Architecture:** Python monorepo (`hailhq.*` namespace) running as four deployables behind docker-compose: `api` (FastAPI), `voicebot` (LiveKit Agents worker), `mcp` (SSE MCP server), plus shared `core` library. Go CLI (`hail`) and Python SDK (`hail-sdk`) are the external surfaces. OpenAPI spec is source of truth for both. Twilio for SIP + numbers; LiveKit Cloud for media; Deepgram/ElevenLabs/Silero for the voice pipeline; OpenAI-compat fallback chain or caller-provided endpoint for the LLM.

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy 2 + Pydantic v2 + Alembic + LiveKit Agents + Twilio SDK + Go (Cobra eventually) + pytest + ruff + black + uv workspace + pnpm.

**Scope note:** This plan covers 13 tasks spanning multiple subsystems. Tasks 1 and 2 are detailed at TDD-step granularity because they're the immediate next work unit. Tasks 3–13 have structural outlines (files, approach, commit message) — their detailed step-by-step plans should be written as `docs/superpowers/plans/YYYY-MM-DD-task-NN-*.md` when each implementation starts, using this plan as context. This keeps the plan honest about what's immediately actionable.

---

## File Structure Overview

Files created or modified across the whole v1 effort. Grouped by task.

**Task 1 — core models + schemas**

- Create: `core/hailhq/core/models.py`, `core/hailhq/core/schemas.py`
- Create: `core/tests/__init__.py`, `core/tests/test_models.py`, `core/tests/test_schemas.py`
- Create: `core/conftest.py`
- Modify: `core/pyproject.toml` (add sqlalchemy)

**Task 2 — CI workflow**

- Create: `.github/workflows/ci.yml`

**Task 3 — provider interface + Twilio voice adapter**

- Create: `core/hailhq/core/providers/__init__.py`, `core/hailhq/core/providers/voice/__init__.py`, `core/hailhq/core/providers/voice/base.py`, `core/hailhq/core/providers/voice/twilio.py`
- Create: `core/tests/providers/test_twilio_voice.py`

**Task 4 — LiveKit integration helpers**

- Create: `core/hailhq/core/livekit.py`
- Create: `core/tests/test_livekit.py`

**Task 5 — API-key auth middleware**

- Create: `api/hailhq/api/auth.py`, `api/hailhq/api/deps.py`
- Create: `api/tests/__init__.py`, `api/tests/test_auth.py`, `api/tests/conftest.py`
- Modify: `api/hailhq/api/main.py`

**Task 6 — POST /calls, GET /calls/{id}, GET /calls**

- Create: `api/hailhq/api/routes/__init__.py`, `api/hailhq/api/routes/calls.py`
- Create: `api/tests/test_calls_api.py`
- Modify: `api/hailhq/api/main.py`, `openapi/openapi.yaml`

**Task 7 — Idempotency middleware**

- Create: `api/hailhq/api/idempotency.py`
- Create: `api/tests/test_idempotency.py`
- Modify: `api/hailhq/api/routes/calls.py`

**Task 8 — Voicebot worker**

- Create: `voicebot/hailhq/voicebot/agent.py`, `voicebot/hailhq/voicebot/pipeline.py`, `voicebot/hailhq/voicebot/recording.py`
- Modify: `voicebot/hailhq/voicebot/main.py`
- Create: `voicebot/tests/__init__.py`, `voicebot/tests/test_pipeline.py`

**Task 9 — CLI implementation**

- Create: `cli/internal/client/` (codegen output), `cli/internal/cmd/call.go`, `cli/internal/cmd/root.go`
- Modify: `cli/main.go`, `cli/go.mod`
- Create: `cli/Makefile` (for `make codegen`)

**Task 10 — MCP place_call tool**

- Modify: `mcp/hailhq/mcp/server.py`
- Create: `mcp/hailhq/mcp/tools.py`, `mcp/hailhq/mcp/hail_client.py`
- Create: `mcp/tests/__init__.py`, `mcp/tests/test_tools.py`

**Task 11 — SDK real client**

- Modify: `sdk/hail/client.py`
- Create: `sdk/hail/models.py`, `sdk/hail/_http.py`
- Create: `sdk/tests/__init__.py`, `sdk/tests/test_client.py`

**Task 12 — CLI release workflow**

- Create: `.github/workflows/release-cli.yml`, `.goreleaser.yml`

**Task 13 — Cut v0.1.0**

- Modify: `CHANGELOG.md` (new file), root `pyproject.toml` (version bump), per-service pyproject.tomls.

---

## Task 1: Core SQLAlchemy models + Pydantic schemas

**Files:**

- Create: `core/hailhq/core/models.py`
- Create: `core/hailhq/core/schemas.py`
- Create: `core/tests/__init__.py`, `core/tests/test_models.py`, `core/tests/test_schemas.py`
- Create: `core/conftest.py`
- Modify: `core/pyproject.toml`

- [ ] **Step 1: Add sqlalchemy to `core/pyproject.toml` and sync**

Edit `core/pyproject.toml`:

```toml
dependencies = [
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "sqlalchemy>=2.0",
]
```

Run:

```bash
uv sync --package hailhq-core --extra dev
```

Expected: `sqlalchemy` + the `dev` extras (pytest, ruff, mypy) installed in the workspace venv. Dropping `--extra dev` leaves pytest unavailable in the venv, so subsequent steps fail with "Failed to spawn pytest".

- [ ] **Step 2: Write `core/conftest.py` providing an in-memory SQLite session for tests**

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture()
def session():
    from hailhq.core.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
```

- [ ] **Step 3: Write the failing model round-trip test**

Create `core/tests/test_models.py`:

```python
from datetime import datetime

from hailhq.core.models import Call, Organization, PhoneNumber


def test_call_round_trip(session):
    org = Organization(name="Acme", slug="acme")
    session.add(org)
    session.flush()

    number = PhoneNumber(
        organization_id=org.id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN123",
    )
    session.add(number)
    session.flush()

    call = Call(
        organization_id=org.id,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+14155559999",
        voice_config={"stt": "deepgram", "tts": "elevenlabs"},
    )
    session.add(call)
    session.commit()

    fetched = session.get(Call, call.id)
    assert fetched is not None
    assert fetched.to_e164 == "+14155559999"
    assert fetched.status == "queued"
    assert isinstance(fetched.created_at, datetime)
```

- [ ] **Step 4: Run the test — expect it to fail with ImportError**

```bash
cd core && uv run pytest tests/test_models.py -v
```

Expected: `ModuleNotFoundError` for `hailhq.core.models`.

- [ ] **Step 5: Write `core/hailhq/core/models.py` — all tables from the migration**

```python
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("ARRAY['*']"), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    e164: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    country_code: Mapped[str] = mapped_column(Text, nullable=False)
    number_type: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("ARRAY['voice','sms']"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, server_default="twilio", nullable=False)
    provider_resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    provisioning_state: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    provisioning_metadata: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    acquired_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)

    __table_args__ = (
        CheckConstraint("number_type IN ('local','mobile','toll_free')", name="phone_numbers_number_type_check"),
        CheckConstraint("provisioning_state IN ('pending','active','failed','released')", name="phone_numbers_state_check"),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    from_number_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("phone_numbers.id"), nullable=False)
    from_e164: Mapped[str] = mapped_column(Text, nullable=False)
    to_e164: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, server_default="outbound", nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="queued", nullable=False)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(Text, server_default="twilio", nullable=False)
    provider_call_sid: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    livekit_room: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    initial_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recording_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"), nullable=False)
```

Note: `UUID(as_uuid=True)` and `JSONB` are Postgres-specific. The SQLite test will fail on them unless we add a compat shim. Easier fix: set `SQLITE_COMPAT = True` pattern via dialect-aware types, OR run tests against a real Postgres via testcontainers. For this plan we'll use a Postgres fixture instead of SQLite — see Step 6.

- [ ] **Step 6: Update `core/conftest.py` to use a real Postgres via testcontainers**

Change deps first — edit `core/pyproject.toml`:

```toml
[project.optional-dependencies]
dev = ["pytest", "ruff", "mypy", "testcontainers[postgres]>=4", "psycopg[binary]>=3.2"]
```

Run: `uv sync --package hailhq-core`.

Replace `core/conftest.py`:

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture()
def session(postgres_container):
    from hailhq.core.models import Base

    url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
```

- [ ] **Step 7: Run the model test again, expect PASS**

```bash
cd core && uv run pytest tests/test_models.py -v
```

Expected: the round-trip test passes. Container spin-up adds ~5s on first run (session-scoped), then fast.

- [ ] **Step 8: Commit the models**

```bash
git add core/hailhq/core/models.py core/pyproject.toml core/conftest.py core/tests/__init__.py core/tests/test_models.py
git commit -m "$(cat <<'EOF'
feat(core): add SQLAlchemy models for v1 tables

Mirrors the initial Alembic migration: organizations, api_keys,
phone_numbers, conversations, calls, call_events, idempotency_keys,
audit_log. Postgres-specific types (UUID, JSONB, INET) — tests use
testcontainers/postgres to exercise the real dialect.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 9: Write failing schema tests**

Create `core/tests/test_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from hailhq.core.schemas import CallCreate, CallResponse, LLMConfig, VoiceConfig


def test_call_create_minimal_valid():
    req = CallCreate(to="+14155551234", system_prompt="Hi")
    assert req.to == "+14155551234"
    assert req.system_prompt == "Hi"
    assert req.llm is None


def test_call_create_with_byo_endpoint():
    req = CallCreate(
        to="+14155551234",
        llm=LLMConfig(base_url="https://x.example/v1", api_key="k", model="m"),
    )
    assert req.llm is not None
    assert req.llm.base_url == "https://x.example/v1"


def test_call_create_rejects_non_e164():
    with pytest.raises(ValidationError):
        CallCreate(to="4155551234", system_prompt="Hi")


def test_call_create_requires_prompt_or_llm():
    with pytest.raises(ValidationError):
        CallCreate(to="+14155551234")


def test_voice_config_defaults():
    cfg = VoiceConfig()
    assert cfg.stt == "deepgram"
    assert cfg.tts == "elevenlabs"
    assert cfg.vad == "silero"
```

Run: `cd core && uv run pytest tests/test_schemas.py -v`
Expected: `ImportError` for `hailhq.core.schemas`.

- [ ] **Step 10: Write `core/hailhq/core/schemas.py`**

```python
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


E164 = re.compile(r"^\+[1-9]\d{1,14}$")


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    model: str


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stt: Literal["deepgram"] = "deepgram"
    tts: Literal["elevenlabs"] = "elevenlabs"
    vad: Literal["silero"] = "silero"
    turn_detection: Literal["livekit"] = "livekit"


class CallCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str
    from_: str | None = Field(default=None, alias="from")
    system_prompt: str | None = None
    llm: LLMConfig | None = None
    first_message: str | None = None
    voice_config: VoiceConfig = Field(default_factory=VoiceConfig)
    conversation_id: UUID | None = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("to", "from_")
    @classmethod
    def _validate_e164(cls, v: str | None) -> str | None:
        if v is not None and not E164.match(v):
            raise ValueError("must be E.164 (e.g. +14155551234)")
        return v

    @model_validator(mode="after")
    def _prompt_or_llm(self):
        if self.system_prompt is None and self.llm is None:
            raise ValueError("either system_prompt or llm must be provided")
        return self


CallStatus = Literal[
    "queued", "dialing", "ringing", "in_progress",
    "completed", "failed", "busy", "no_answer", "canceled",
]


class CallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    conversation_id: UUID | None
    from_e164: str
    to_e164: str
    direction: Literal["outbound", "inbound"]
    status: CallStatus
    end_reason: str | None
    provider_call_sid: str | None
    livekit_room: str | None
    initial_prompt: str | None
    recording_s3_key: str | None
    requested_at: datetime
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None


class CallListResponse(BaseModel):
    items: list[CallResponse]
    next_cursor: str | None = None
```

- [ ] **Step 11: Run schema tests — expect PASS**

```bash
cd core && uv run pytest tests/test_schemas.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 12: Run full core test suite**

```bash
cd core && uv run pytest -v
```

Expected: all tests pass.

- [ ] **Step 13: Commit schemas**

```bash
git add core/hailhq/core/schemas.py core/tests/test_schemas.py
git commit -m "$(cat <<'EOF'
feat(core): add Pydantic schemas for the calls API

CallCreate / CallResponse / CallListResponse plus VoiceConfig and
LLMConfig (mode A = system_prompt with Hail fallback chain, mode B =
caller-provided OpenAI-compat endpoint). E.164 validation. One of
system_prompt / llm must be supplied.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: CI workflow

**Files:**

- Create: `.github/workflows/ci.yml`
- Modify: `core/conftest.py` — honor an external `DATABASE_URL` env var before spinning up testcontainers, so CI uses its Postgres service container directly (no Docker-in-Docker needed).

- [ ] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          python-version: "3.12"
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run black --check .

  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: hail
          POSTGRES_PASSWORD: hail
          POSTGRES_DB: hail
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U hail"
          --health-interval 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          python-version: "3.12"
      - run: uv sync --all-packages
      - run: uv run pytest core/tests api/tests voicebot/tests mcp/tests sdk/tests -v
        env:
          DATABASE_URL: postgresql+psycopg://hail:hail@localhost:5432/hail

  go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: "1.23"
      - working-directory: cli
        run: |
          go vet ./...
          go test ./...
          go build ./...

  docker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - run: docker compose build api voicebot mcp
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
ci: lint, test, go, and docker smoke on every PR

- lint: ruff + black check across the uv workspace
- test: pytest with a Postgres 16 service container
- go: vet + test + build on cli/
- docker: compose build smoke for api, voicebot, mcp

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Push and verify green**

```bash
git push
```

Watch the run at `https://github.com/hail-hq/hail/actions`. Expected: all four jobs pass. If `test` or `docker` jobs depend on files that don't exist yet (e.g., tests), the failures are informative and get fixed as subsequent tasks land.

- [ ] **Step 4: Verify the workflow catches a broken test**

Temporarily edit any test to fail, push on a branch, confirm the `test` job goes red, revert, confirm green. (Sanity check only — don't commit the break.)

---

## Task 3: Provider interface + Twilio voice adapter

**Files:**

- Create: `core/hailhq/core/providers/__init__.py`, `core/hailhq/core/providers/voice/__init__.py`, `core/hailhq/core/providers/voice/base.py`, `core/hailhq/core/providers/voice/twilio.py`
- Create: `core/tests/providers/__init__.py`, `core/tests/providers/test_twilio_voice.py`
- Modify: `core/pyproject.toml` (add `twilio>=9.3` as runtime dep)

**Approach:**

- `voice/base.py`: abstract `VoiceProvider` with methods `acquire_number(country, number_type)`, `release_number(resource_id)`, `create_sip_call(from_e164, to_e164, livekit_trunk_uri, call_id)` returning a `ProviderCall` record with `provider_call_sid`.
- `voice/twilio.py`: concrete implementation using `twilio.rest.Client`. Reads credentials from `hailhq.core.config.settings`.
- Tests: use `responses` or `respx` to mock Twilio's HTTP API; assert the adapter calls the right endpoints with the right payload. Integration tests (live Twilio) are gated on `HAIL_TWILIO_LIVE=1` and skipped by default.

**Commit message:**

```
feat(core): Twilio voice provider adapter

VoiceProvider interface in providers/voice/base.py with one concrete
implementation (twilio.py). acquire_number, release_number, and
create_sip_call match the v1 surface. Unit-tested via respx mocks;
live tests gated by HAIL_TWILIO_LIVE.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 4: LiveKit integration helpers

**Files:**

- Create: `core/hailhq/core/livekit.py`, `core/tests/test_livekit.py`
- Modify: `core/pyproject.toml` (add `livekit-api>=0.8`)

**Approach:**

- `livekit.py` exports `create_room(call_id) -> room_name`, `dispatch_agent(room_name, agent_name, metadata: dict) -> AgentJob`, `build_sip_participant_uri(number_e164)`.
- Uses `livekit.api.LiveKitAPI` with `LIVEKIT_URL/KEY/SECRET` from settings.
- Tests: mock `LiveKitAPI` methods; verify `room_service.create_room` + `agent_dispatch.create_dispatch` are called with expected arguments.

**Commit message:**

```
feat(core): LiveKit room/agent-dispatch helpers

Thin wrappers around livekit-api for create_room and dispatch_agent.
Keeps LiveKit auth + room-naming conventions in one place so api and
voicebot don't each reimplement them.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 5: API-key auth middleware

**Files:**

- Create: `api/hailhq/api/auth.py`, `api/hailhq/api/deps.py`
- Create: `api/tests/__init__.py`, `api/tests/conftest.py`, `api/tests/test_auth.py`
- Modify: `api/hailhq/api/main.py`

**Approach:**

- `auth.py`: `hash_key(plain: str) -> (prefix, hash)` using SHA-256; `verify_key(plain, stored_hash) -> bool`.
- `deps.py`: FastAPI dependency `get_current_key(request, db: Session)` that parses `Authorization: Bearer <key>`, looks up by `key_hash`, updates `last_used_at`, returns the `ApiKey` + associated `Organization`. Raises 401 on miss; 403 if expired.
- Writes an `audit_log` row.
- Tests: fixture creates an org + api_key; test unauthenticated 401, bad-key 401, valid-key 200, expired-key 403.

**Commit message:**

```
feat(api): API-key auth middleware

Bearer-token flow against api_keys.key_hash (SHA-256). Updates
last_used_at and writes an audit_log row per request. FastAPI
dependency `require_api_key` returns the owning org for handlers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 6: `POST /calls`, `GET /calls/{id}`, `GET /calls`

**Files:**

- Create: `api/hailhq/api/routes/__init__.py`, `api/hailhq/api/routes/calls.py`
- Create: `api/tests/test_calls_api.py`
- Modify: `api/hailhq/api/main.py` (mount the router)
- Modify: `openapi/openapi.yaml` (regenerate after route changes)

**Approach:**

- `POST /calls`: accepts `CallCreate`, resolves `from_e164` to a `PhoneNumber`, creates `Call` row with `voice_config` snapshot and `status="queued"`, calls `livekit.create_room` + `dispatch_agent`, triggers `twilio.create_sip_call`, returns `CallResponse`. All inside one DB transaction with the provider calls deferred to `after_commit` hook to avoid orphaned Twilio calls if commit fails.
- `GET /calls/{id}`: auth → scope-check organization → return `CallResponse`.
- `GET /calls`: cursor pagination (created_at, id), filter by `status`, `to_e164`. Returns `CallListResponse`.
- Regenerate OpenAPI: `cd api && uv run python -c "from hailhq.api.main import app; import json, sys, yaml; yaml.safe_dump(app.openapi(), sys.stdout, sort_keys=False)" > ../openapi/openapi.yaml`.
- Tests: async client (httpx.AsyncClient w/ ASGI transport), mocked Twilio + LiveKit. Assert DB rows + provider calls.

**Commit message:**

```
feat(api): POST /calls and call-retrieval routes

- POST /calls creates a Call row, provisions a LiveKit room, dispatches
  the voicebot agent, and places the SIP outbound via Twilio.
- GET /calls/{id} and GET /calls with cursor pagination.
- openapi/openapi.yaml regenerated.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 7: Idempotency middleware

**Files:**

- Create: `api/hailhq/api/idempotency.py`, `api/tests/test_idempotency.py`
- Modify: `api/hailhq/api/routes/calls.py` (opt-in on POST)

**Approach:**

- Reads `Idempotency-Key` header; normalized request body hashed (SHA-256). If `(org_id, key)` row exists:
  - same `request_hash` → return cached `response_status` + `response_body`
  - different hash → 409 "idempotency key reused with different body"
- On first request, persist response after handler returns.
- TTL: 24 hours (column default). Expired rows cleaned up by a separate process (future cron).
- Tests: same-key/same-body returns cached; same-key/different-body → 409; no key → normal flow.

**Commit message:**

```
feat(api): idempotency middleware for POST /calls

Idempotency-Key header; (org_id, key) indexed in idempotency_keys
table. Request-hash mismatch returns 409. 24h TTL; GC by future job.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 8: Voicebot worker

**Files:**

- Create: `voicebot/hailhq/voicebot/agent.py`, `voicebot/hailhq/voicebot/pipeline.py`, `voicebot/hailhq/voicebot/recording.py`
- Create: `voicebot/tests/__init__.py`, `voicebot/tests/test_pipeline.py`
- Modify: `voicebot/hailhq/voicebot/main.py` (real entrypoint replacing the NotImplementedError stub)

**Approach:**

- `main.py`: `livekit.agents.cli.run_app(WorkerOptions(entrypoint=entrypoint, agent_name="hail-voicebot"))`.
- `agent.py`: reads `JobContext.metadata` (set by api when dispatching) to get `call_id`, `voice_config`, `system_prompt` or `llm` config, `first_message`. Fetches call from DB.
- `pipeline.py`: builds `AgentSession(vad=silero.VAD.load(), stt=deepgram.STT(), tts=elevenlabs.TTS(), llm=_build_llm(cfg))`. `_build_llm`: mode A uses `livekit.agents.llm.FallbackAdapter([openai.LLM("gpt-4o-mini"), google.LLM("gemini-1.5-flash"), anthropic.LLM("claude-haiku-4-5")])`; mode B uses `openai.LLM(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model)`.
- On session events, append to `call_events`.
- On hangup: `recording.upload_to_s3(room, call_id)`, update `calls.recording_s3_key` + `recording_duration_ms` + `status="completed"` + `ended_at`.
- Tests (unit): `_build_llm(mode_a_cfg)` returns `FallbackAdapter` instance, `_build_llm(mode_b_cfg)` returns configured `openai.LLM`. End-to-end via LiveKit test helpers is future work.

**Commit message:**

```
feat(voicebot): LiveKit Agents worker with voice pipeline

Silero VAD + Deepgram STT + ElevenLabs TTS + (FallbackAdapter or
BYO-endpoint) LLM. Reads dispatch metadata, writes call_events and
recording to S3 on hangup.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 9: CLI implementation

**Files:**

- Create: `cli/internal/client/*` (generated by oapi-codegen), `cli/internal/cmd/root.go`, `cli/internal/cmd/call.go`, `cli/Makefile`
- Modify: `cli/main.go`, `cli/go.mod`

**Approach:**

- `cli/Makefile`: `codegen` target runs `go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2 -package client -generate types,client -o internal/client/client.gen.go ../openapi/openapi.yaml`.
- Use `github.com/spf13/cobra` for subcommands.
- `call.go`: `hail call <to-number> --prompt "..."` or `--llm-url --llm-key --llm-model`. Reads `HAIL_API_URL` + `HAIL_API_KEY` from env. POSTs via generated client, prints JSON or human-friendly.
- Tests: `go test` with httptest.NewServer as fake API.

**Commit message:**

```
feat(cli): hail call subcommand with OpenAPI-generated client

oapi-codegen emits internal/client/ from openapi/openapi.yaml.
`hail call <num> --prompt "..."` POSTs /calls. Cobra for subcommand
routing. make codegen regenerates the client.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 10: MCP `place_call` tool

**Files:**

- Modify: `mcp/hailhq/mcp/server.py` (real MCP app, not the FastAPI stub)
- Create: `mcp/hailhq/mcp/tools.py`, `mcp/hailhq/mcp/hail_client.py`
- Create: `mcp/tests/__init__.py`, `mcp/tests/test_tools.py`

**Approach:**

- Use `mcp.server.fastmcp.FastMCP`. Expose `place_call(to: str, system_prompt: str | None = None, llm: dict | None = None, first_message: str | None = None) -> dict` via `@mcp_app.tool()`.
- `hail_client.py`: thin httpx client that POSTs to `HAIL_API_URL/calls` with `HAIL_API_KEY` bearer.
- `server.py`: `app = mcp_app.sse_app()` — Starlette/FastAPI ASGI app, hooks into existing `uvicorn` CMD.
- Tests: call `place_call` with mocked httpx, verify request shape + response mapping.

**Commit message:**

```
feat(mcp): place_call tool wrapping the Hail API

FastMCP SSE app replaces the FastAPI healthz stub. hail_client.py
holds the httpx logic; tools.py registers place_call. The MCP service
remains a remote-SSE endpoint — no stdio (see docs/setup/mcp.md).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 11: SDK real client

**Files:**

- Modify: `sdk/hail/client.py`
- Create: `sdk/hail/models.py`, `sdk/hail/_http.py`
- Create: `sdk/tests/__init__.py`, `sdk/tests/test_client.py`
- Modify: `sdk/pyproject.toml` (version → 0.1.0)

**Approach:**

- `models.py`: Pydantic models mirroring `hailhq.core.schemas` but duplicated to keep SDK install dep-light (does not import hailhq-core). Revisit when OpenAPI codegen is in place.
- `_http.py`: httpx transport. Retries on 5xx, honors `Idempotency-Key` header.
- `client.py`: `Client.calls.create(...)`, `Client.calls.get(id)`, `Client.calls.list(...)`. Matches OpenAPI.
- Tests: respx-mocked; request-shape + response-parse assertions.

**Commit message:**

```
feat(sdk): v0.1.0 — real Client.calls.create / get / list

httpx-backed client with retries + idempotency key support. Models
mirror the v1 OpenAPI spec. Bumps hail-sdk from 0.0.1 (placeholder) to
0.1.0. Tag sdk-v0.1.0 to publish.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 12: CLI release workflow

**Files:**

- Create: `.github/workflows/release-cli.yml`, `.goreleaser.yml`

**Approach:**

- `.goreleaser.yml`: builds darwin/linux × amd64/arm64, packages as tarballs, generates a Homebrew tap formula targeting `hail-hq/homebrew-tap`.
- `release-cli.yml`: triggers on `cli-v*` tags; runs goreleaser with a GH token that has write access to `homebrew-tap`.

**Commit message:**

```
feat(ci): CLI release via GoReleaser on cli-v* tags

Multi-arch binaries to GitHub Releases plus an auto-generated formula
pushed to hail-hq/homebrew-tap so `brew install hail-hq/tap/hail`
works on first release.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Task 13: Cut v0.1.0

**Files:**

- Create: `CHANGELOG.md`
- Modify: `api/pyproject.toml`, `voicebot/pyproject.toml`, `mcp/pyproject.toml`, `core/pyproject.toml` (all → 0.1.0)
- Modify: `README.md` (tick milestones)

**Approach:**

1. Write initial `CHANGELOG.md` grouped per v1 milestones: Phone calls (Twilio outbound), Voice pipeline, Distribution (CLI `hail`, MCP SSE endpoint, Python SDK `hail-sdk`), Infrastructure (Docker Compose scaffold).
2. Version bump commit.
3. `git tag v0.1.0` + `git push origin v0.1.0`. Manually verify: PyPI, Docker images (if CI handles them), GitHub Release (if CI handles), Homebrew tap updated.
4. Tick the `[ ]`→`[x]` milestones in README for everything that shipped.

**Commit message:**

```
chore: cut v0.1.0

Outbound Twilio voice calls end-to-end: POST /calls → dispatch
voicebot → SIP out. CLI, Python SDK, and MCP server all live.
See CHANGELOG for the full manifest.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Self-review notes

1. **Spec coverage**: every task in the previous "next steps" roadmap (items 1–13) has a section. ✅
2. **Placeholders**: None of the "TBD / implement later / similar to Task N / write tests for the above" patterns remain in Tasks 1–2 (full detail). Tasks 3–13 are explicitly scoped as structural outlines — detailed TDD plans to be written per task before execution, which is itself an explicit process, not a placeholder. ✅
3. **Type consistency**:
   - Pydantic schema `CallCreate` referenced in Task 1, used unchanged in Tasks 6 and 11. ✅
   - `VoiceProvider` interface in Task 3, consumed in Task 6. ✅
   - `dispatch_agent` in Task 4, called from Task 6. ✅
   - `voice_config` JSONB in migration + `VoiceConfig` Pydantic — field names (`stt`, `tts`, `vad`, `turn_detection`) consistent. ✅
   - `conversations.metadata` column collides with SQLAlchemy's reserved `Base.metadata`; worked around via `"metadata"` named-column (see Task 1 code). ✅

## Open decisions deferred to later plans

- **OpenAPI → SDK model generation**: Task 11 duplicates schemas by hand. A later pass can generate `sdk/hail/models.py` from `openapi/openapi.yaml` (openapi-python-client).
- **Autogenerate Alembic migrations**: Task 1 only ships models (not metadata-wired env.py). When the next migration is needed, wire `target_metadata = Base.metadata` in `api/migrations/env.py`.
- **Service identity for audit trails**: api + voicebot + mcp each need a consistent `actor_type` pattern in `audit_log` — TBD by Task 5 detailed plan.
- **Error model**: FastAPI exception handlers → structured error responses — TBD by Task 6 detailed plan.
