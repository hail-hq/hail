# Changelog

All notable changes to Hail are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Hail adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-04-30

First public release. Outbound phone calls for AI agents, end-to-end.

### Phone calls

- Outbound calls via Twilio SIP through LiveKit Cloud.
- `POST /calls`, `GET /calls/{id}`, and `GET /calls` with cursor pagination.
- Per-org API-key auth with audit logging on every authenticated request.
- Idempotency-Key support on `POST /calls` (24h TTL).

### Voice pipeline

- Deepgram STT.
- ElevenLabs TTS.
- Silero VAD (prewarmed once per worker process).
- LiveKit turn-detector for end-of-utterance detection.
- LLM system-prompt mode with OpenAI to Gemini to Anthropic fallback chain.
- LLM BYO-endpoint mode for any OpenAI chat-completions-compatible endpoint.
- Per-turn `call_events` rows for transcript reconstruction.

### Distribution

- OpenAPI 3.1 spec at `openapi/openapi.yaml` as the source of truth.
- `hail` CLI binary published via GitHub Releases (darwin and linux, amd64 and arm64) and a Homebrew tap.
- Remote MCP server bundled with every Hail deploy at `/sse`, exposing a `place_call` tool.
- `hail-sdk` on PyPI (imports as `hail`) with `Client.calls.create / get / list`.

### Infrastructure

- `docker compose up` brings up `api`, `voicebot`, `mcp`, and Postgres.
- Alembic migrations for the v1 schema.
- CI runs lint, pytest with a Postgres service container, Go build and test, and a docker-compose build smoke.

### Deferred to v1.x

- LiveKit Egress recording (`recording.py` is stubbed; `Call.recording_s3_key` is always `NULL`).
- `idempotency_keys` GC sweeper and in-flight reaper.
- Inbound calls (`LIVEKIT_SIP_INBOUND_TRUNK_ID` is reserved in env but unused).
- SMS channel (Twilio outbound and inbound).
- Email channel (AWS SES outbound and inbound).
- `CallEvent` dedupe across voicebot redispatch.

[0.1.0]: https://github.com/hail-hq/hail/releases/tag/v0.1.0
