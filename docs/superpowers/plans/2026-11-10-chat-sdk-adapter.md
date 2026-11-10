# Hail Chat SDK adapter

## Outcome and scope

Publish an official Hail-owned npm adapter and submit it for Vercel's vendor-official directory.
The first release connects SMS to Chat SDK; email, voice, MMS, RCS, and iMessage remain outside its scope.
Use Hail's existing public API and signed webhooks without changing services, databases, infrastructure, or production routing.

## Implementation

- Keep the independently versioned Node package and pnpm workspace in `integrations/chat-sdk`.
- Implement the Chat SDK `Adapter` interface, literal inbound SMS, text replies, phone-pair thread and channel isolation, and single-message reads.
- Resolve API and webhook credentials per call. Require signatures and replay freshness; require explicit recipient consent before sending.
- Enable Chat SDK's state-backed history fallback instead of scanning unrelated organization messages.
- Supply a Node-runtime Next.js example with persistent state required in production.
- Add unit/integration coverage, package validation CI, Hail product docs, and draft release materials.
- Prepare Vercel's documentation page, catalog metadata, scaffold registration, and changeset against the current upstream source.

## Verification and completion gates

Run the package tests, type checker, ESM/declaration build, example type checker and build, package-content check, and Codex review.
Validate Vercel's catalog, scaffold, and documentation parity using upstream checks; report any unrelated failures.
Commit and push only task branches. Open the Hail PR for review.
Do not submit an incomplete listing: npm publication, live product docs, a public announcement, ongoing Hail maintenance, and verified commit signing are required before the Vercel PR.
The current machine has no npm login; publication may require the user to authenticate after reviewing the concrete package.
No merges, deployments, production writes, or test SMS sends are authorized by this implementation plan.
