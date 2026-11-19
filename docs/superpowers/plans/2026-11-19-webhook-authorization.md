# Webhook Authorization Implementation Plan

> Execute inline with superpowers:executing-plans; review each final branch.

**Goal:** Deliver Hail webhook events to receivers requiring Authorization.
**Architecture:** Extend webhook_subscriptions with encrypted authorization;
use the existing cipher and worker. The console sends write-only values.
**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, PostgreSQL, Next.js, React.
**Spec:** ../specs/2026-11-19-webhook-authorization-design.md

## Global Constraints

Keep existing HMAC signatures and organization ownership checks. No production
writes, deployments, replay, or merges. Deploy the backend before the console.

## Review Focus

- URL edits must never carry a saved credential to an unintended receiver.
- Invalid or reflected secrets must not reach API responses or delivery history.
- Unrelated edits must preserve saved credentials; empty values remove them.
- Paused subscriptions remain paused after changing authentication.
- Existing subscriptions without credentials remain usable.

### Task 1: Backend

- [ ] Add failing API tests for encrypted create/read, PATCH preserve/remove,
  URL-change protection, and invalid-header privacy in api/tests/test_webhooks_api.py.
- [ ] Add failing authenticated delivery and reflected-secret tests in
  core/tests/test_webhook_worker.py. Run both files and verify expected failures.
- [ ] Extend models.py and migration 0087; add SecretStr request fields and
  has_authorization response; encrypt in routes/webhooks.py and decrypt in worker.
- [ ] Run API/worker/schema tests, lint, and migration checks; regenerate OpenAPI.
- [ ] Document receiver authorization and commit the tested backend.

### Task 2: Console

- [ ] Add failing action and form tests for create, preserve, replace, remove,
  URL-change reentry, and clearing typed credentials.
- [ ] Extend webhook-queries.ts with a boolean only; map authorization in actions.ts;
  add a password field with saved-state controls in WebhooksClient.tsx.
- [ ] Run webhook/query tests, lint, and the production build.
- [ ] Render fixture previews in both themes at desktop and phone widths.
- [ ] Commit, review both branches, push, and open linked draft PRs.

## Progress

Starting from backend 22b0b22d8 and current website origin/master.

Backend: 2821 core tests passed, 21 skipped. Webhook and idempotency API:
37 passed. A local HTTP receiver returned 401 without authorization, then
204 for the encrypted credential plus HMAC. Migration 0087 passed
upgrade/downgrade/upgrade on local PostgreSQL, preserving an existing target.

Console: 45 webhook/query/design-rule tests passed; ESLint and the production
build passed. Fixture rendering and interactions passed at 1500px, 390px, and
320px under both color preferences; screenshots inspected. The current console
uses its existing light palette under both preferences. No production writes or
live Cursor requests were made. Independent committed-branch review follows.
