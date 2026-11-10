# Release and submit the Hail adapter

The adapter is prepared as `@hail-hq/chat-sdk-adapter@0.1.0`. This document is a release checklist, not a publication record.

## Validate the artifact

From this directory:

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm typecheck
pnpm build
pnpm --filter example-hail-chat-sdk typecheck
# Build-only placeholders; this does not start Redis or send SMS.
REDIS_URL=redis://localhost:6379 HAIL_FROM_NUMBER=+14155550100 pnpm --filter example-hail-chat-sdk build
npm pack --dry-run
```

Chat SDK is MIT-licensed and compatible with the adapter's AGPL-3.0-only license.
The package contains its ESM build, TypeScript declarations, README, and license.
Before publication, test an actual signed delivery and reply using an authorized test number and credits.
The automated tests use the real Chat runtime and a fake Hail HTTP transport; they do not establish carrier delivery.

## Publish the package

After the Hail PR is approved and incorporated by a maintainer:

1. Authenticate npm with an account that can publish under `@hail-hq`: `npm login`.
2. Confirm the account with `npm whoami` and confirm its `@hail-hq` organization permissions.
3. Run `npm publish --access public` from this directory. The `prepack` script runs tests, type checking, and the build.
4. Confirm `npm view @hail-hq/chat-sdk-adapter@0.1.0 version repository dist.integrity` and install the published artifact into a clean app.
5. Confirm the Hail product documentation at `https://hail.so/docs/webhooks#chat-sdk` is live. Publish the reviewed announcement from `ANNOUNCEMENT.md` and save its public URL.

Do not place an npm token in the repository. There is no automatic publish or deployment workflow in this change.

## Submit the Vercel listing

Use the prepared `vercel-chat.patch` against the Vercel source revision recorded in the patch.
Check current upstream requirements before applying it. The patch adds the MDX page, catalog entry, CLI scaffold, and changeset.
Current docs tests derive allowed packages and adapter pages from the catalog, so there are no static allowlists to extend.

Follow [Vercel's vendor-official requirements](https://chat-sdk.dev/docs/contributing/vendor-official):

- The npm package must already be published.
- The public Hail docs must cover the adapter, and the announcement must be public.
- Hail must commit to maintenance and developer support.
- The source must remain in Hail's official GitHub organization.
- The APIs must be public, not closed beta.
- The listing README URL must stay pinned to the reviewed source revision.

Run the upstream catalog, CLI, and integration checks before opening a listing PR against `vercel/chat`.
Include the published package, pinned source, product docs, and announcement links.
Vercel requires verified signed commits and DCO sign-off. No SSH signing keys were registered for the current GitHub account when checked; verify commit signing before submission.
The listing does not grant Vercel Connect registration or Vercel maintenance.

## Maintenance

Keep the package, README, Hail docs, Vercel catalog, and scaffold configuration in sync when the adapter changes.
Test against new Chat SDK releases. Handle adapter bug reports in the Hail repository.
