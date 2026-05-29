# Better Auth OAuth Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `hail-website` into the OAuth 2.1 Authorization Server for the Hail MCP ecosystem — enabling Better Auth's `oauth-provider` + `mcp` + `jwt` plugins, configuring DCR + PKCE + audience-bound JWTs with the TTL targets from the spec, building the consent page, and surfacing authorized apps in the console.

**Architecture:** Three Better Auth plugins added to `hail-website/lib/auth.ts` — `oauthProvider()` mints OAuth tokens, `jwt()` exposes the `/api/auth/jwks` endpoint and signs JWTs with Better Auth's default **EdDSA / Ed25519** (which the API was aligned to in a small follow-up to 1a), and `mcp()` adds the `.well-known/oauth-protected-resource` discovery the MCP server will point at in 1c. Better Auth migrations add `oauth_clients`, `oauth_access_tokens`, `oauth_refresh_tokens`, `oauth_consents`, and `jwks` tables — all aliased to snake_case to match the existing repo convention. A consent page at `/consent` completes the authorization-code flow; a new console route at `/console/apps` lets users see and revoke authorized applications.

**Tech Stack:** Next.js 16 (per `hail-website/AGENTS.md`: _"This is NOT the Next.js you know — read `node_modules/next/dist/docs/` before writing any code"_), React 19, Better Auth 1.6.9, `@better-auth/oauth-provider` (new dep), `@better-auth/api-key` (already present), `pg` for Postgres, Vitest for tests, pnpm.

**Spec:** [`2026-05-28-mcp-multi-tenant-auth-design.md`](../specs/2026-05-28-mcp-multi-tenant-auth-design.md) — §"1b — hail-website becomes the Authorization Server" and Decision #6 (TTL targets).

---

## Background the implementer needs

- `hail-website/lib/auth.ts` is the single Better Auth config file — currently mounts `lastLoginMethod`, `apiKey` (with `defaultPrefix: "hl_live_"`), `deviceAuthorization`, `bearer`, `organization`, and optionally `polar`. Snake_case schema mapping lives inline per plugin (`schema: { … modelName: "table_name", fields: { camelKey: "snake_col" } }`). New plugins follow the same idiom.
- The Better Auth handler is auto-mounted by the existing Next.js `app/api/auth/[...all]/route.ts` catch-all; OAuth routes (`/oauth2/authorize`, `/oauth2/token`, `/oauth2/register`, etc.) appear there automatically once the plugin is enabled. **Do not add additional API route files.**
- Migrations are SQL files in `hail-website/better-auth_migrations/`. There is exactly one today (`2026-05-12T18-50-04.556Z.sql`). The Better Auth CLI generates new ones with `npx @better-auth/cli generate --output better-auth_migrations/<timestamp>.sql` — the implementer must verify the exact command at write time by running `npx @better-auth/cli --help`. Migrations are committed to the repo; the production deploy runs them via the standard `pg` migration path.
- Pre-existing Better Auth tables (snake_case): `users`, `sessions`, `accounts`, `api_keys`, `device_codes`, `organizations`, `members`, `invitations`, `verifications`.
- AGENTS.md warning: Next.js 16 has breaking changes from prior versions. The implementer **must** check `hail-website/node_modules/next/dist/docs/` for routing/server-component conventions before writing the consent page or the console panel.
- Tests use Vitest (config at `hail-website/vitest.config.ts`). pnpm is the package manager (pinned via `packageManager` in `package.json`). Pre-commit hook runs prettier/eslint on staged files.
- `core/hailhq/core/models.py:ApiKey` and `OrganizationMember` are read-only Python mirrors of `api_keys` and `members`. The new OAuth tables are read by _no_ Python service in Phase 1 (the API validates the _JWT_, not the access-token row); 1c only adds the MCP forwarder. The tables exist solely to back the AS's storage and the console panel.

## Key cross-cutting decisions

- **JWT algorithm = EdDSA / Ed25519** (Better Auth's default; the API was aligned to it in a follow-up to 1a). No explicit `alg` override in the `jwt()` plugin config. Task 4's discovery test asserts that JWKS keys carry `alg: "EdDSA"` / `kty: "OKP"`, so any silent drift back to RS256 (or anywhere else) goes red.
- **`oauth-provider` issues JWT access tokens (default).** The plugin defaults to JWT when the `jwt` plugin is present and `disableJwtPlugin` is false (the default). This is what we want — the API verifies via JWKS, no introspection round-trip.
- **Refresh-token rotation is on by default** ("new refresh token for every refresh request" per docs). Keep it.
- **PKCE is mandatory** (`require_pkce` defaults to true per docs). Confirm explicit configuration anyway.
- **DCR is unauthenticated** (`allowUnauthenticatedClientRegistration: true`) per spec — required for MCP web Connectors that haven't been pre-registered.
- **TTL targets:** `accessTokenExpiresIn: 30 days`, `refreshTokenExpiresIn: 180 days`. Better Auth accepts these as strings (e.g., `"30d"`) or seconds — the implementer verifies the expected type at write time and uses the matching form.
- **Audience binding:** `validAudiences: [<API base>, <MCP base>]` — both `BETTER_AUTH_ISSUER`-relative and `MCP_RESOURCE_URL` per spec. The `mcp` plugin's `resource` option sets the default `aud` for MCP-flow tokens.
- **`disabledPaths: ["/token"]`** is required at the `betterAuth({...})` top level when `oauth-provider` is enabled (per docs' minimal example) — the OAuth `/oauth2/token` endpoint replaces the default Better Auth `/token`.

## File Structure

- **Modify** `hail-website/package.json` + lockfile — add `@better-auth/oauth-provider` (latest 1.x).
- **Modify** `hail-website/lib/auth.ts` — add `jwt()`, `oauthProvider()`, `mcp()` to the plugins array (in that order), with snake_case schema aliases for the five new tables; set `disabledPaths: ["/token"]` at the top level. Existing plugins untouched.
- **Create** `hail-website/better-auth_migrations/<timestamp>.sql` — generated by `@better-auth/cli generate`, committed verbatim after verifying it creates the expected tables/indexes.
- **Create** `hail-website/app/consent/page.tsx` and `app/consent/actions.ts` — the OAuth consent screen + server action that flips the consent record.
- **Create** `hail-website/app/console/apps/page.tsx` + `AppsClient.tsx` + `actions.ts` — list/revoke authorized apps for the active org. Follows the existing `app/console/keys/` pattern verbatim.
- **Create** `hail-website/tests/oauth-discovery.test.ts` — Vitest test exercising the discovery routes and a round-trip JWT verification.
- **Modify** `hail-website/.env.example` — add `MCP_RESOURCE_URL` for the `mcp()` plugin's `resource` option.

---

### Task 1: Add the three plugins and dependency

**Files:**

- Modify: `hail-website/package.json` (+ `pnpm-lock.yaml`)
- Modify: `hail-website/lib/auth.ts`
- Modify: `hail-website/.env.example`

Stage the plugin wiring before generating migrations. The app may fail to start until Task 2 runs the migration; that's expected — Task 2 is the very next task.

- [ ] **Step 1: Install `@better-auth/oauth-provider`.**

Run from `hail-website/`:

```
pnpm add @better-auth/oauth-provider
```

Expected: `pnpm-lock.yaml` updated; `@better-auth/oauth-provider` appears in `dependencies` of `package.json`. Verify version compatibility with the existing `better-auth@^1.6.9` — the package's peer-deps require Better Auth 1.6+.

- [ ] **Step 2: Add the three plugins to `lib/auth.ts`.**

In `hail-website/lib/auth.ts`:

(a) Add this top-level option to the `betterAuth({...})` call (alongside `baseURL`, `database`, `advanced`):

```ts
  disabledPaths: ["/token"],
```

(b) Add these imports at the top of the file, next to the existing `better-auth/plugins` imports:

```ts
import { jwt, mcp } from "better-auth/plugins";
import { oauthProvider } from "@better-auth/oauth-provider";
```

(c) Add these three plugins to the `plugins: [...]` array — **immediately before** the existing `apiKey({...})` entry (so the JWT plugin is initialized before anything that might depend on its `/jwks` endpoint):

```ts
    jwt({
      // Algorithm: Better Auth default = EdDSA / Ed25519. The Python API
      // (api/hailhq/api/auth.py:verify_jwt) was aligned to EdDSA in a
      // follow-up to Phase 1a so we don't override the default here.
      schema: {
        jwks: {
          modelName: "jwks",
          fields: {
            createdAt: "created_at",
            expiresAt: "expires_at",
            publicKey: "public_key",
            privateKey: "private_key",
          },
        },
      },
    }),
    oauthProvider({
      loginPage: "/signin",
      consentPage: "/consent",
      allowDynamicClientRegistration: true,
      allowUnauthenticatedClientRegistration: true,
      // 30 days / 180 days per spec Decision #6 (TTLs as long as feasible).
      accessTokenExpiresIn: "30d",
      refreshTokenExpiresIn: "180d",
      // Audiences accepted by the API + the MCP server (both first-party).
      validAudiences: [baseUrl, process.env.MCP_RESOURCE_URL ?? "http://localhost:8081"],
      schema: {
        oauthClient: {
          modelName: "oauth_clients",
          fields: {
            createdAt: "created_at",
            updatedAt: "updated_at",
            clientId: "client_id",
            clientSecret: "client_secret",
            skipConsent: "skip_consent",
            enableEndSession: "enable_end_session",
            subjectType: "subject_type",
            userId: "user_id",
            referenceId: "reference_id",
            redirectUris: "redirect_uris",
            postLogoutRedirectUris: "post_logout_redirect_uris",
            tokenEndpointAuthMethod: "token_endpoint_auth_method",
            grantTypes: "grant_types",
            responseTypes: "response_types",
            requirePKCE: "require_pkce",
          },
        },
        oauthRefreshToken: {
          modelName: "oauth_refresh_tokens",
          fields: {
            createdAt: "created_at",
            expiresAt: "expires_at",
            clientId: "client_id",
            sessionId: "session_id",
            userId: "user_id",
            referenceId: "reference_id",
            authTime: "auth_time",
          },
        },
        oauthAccessToken: {
          modelName: "oauth_access_tokens",
          fields: {
            createdAt: "created_at",
            expiresAt: "expires_at",
            clientId: "client_id",
            sessionId: "session_id",
            refreshId: "refresh_id",
            userId: "user_id",
            referenceId: "reference_id",
          },
        },
        oauthConsent: {
          modelName: "oauth_consents",
          fields: {
            createdAt: "created_at",
            updatedAt: "updated_at",
            userId: "user_id",
            clientId: "client_id",
            referenceId: "reference_id",
          },
        },
      },
    }),
    mcp({
      loginPage: "/signin",
      resource: process.env.MCP_RESOURCE_URL ?? "http://localhost:8081",
    }),
```

(d) Leave the existing `apiKey()`, `deviceAuthorization()`, `bearer()`, `organization()`, and conditional Polar plugin entries unchanged below this block.

- [ ] **Step 4: Add `MCP_RESOURCE_URL` to `.env.example`.**

Append to `hail-website/.env.example`, in the appropriate section near the other `HAIL_*` URL vars:

```
# Public URL of the MCP server, used as the OAuth `resource`/`aud` for
# MCP-flow tokens. Defaults to http://localhost:8081 in dev.
MCP_RESOURCE_URL=http://localhost:8081
```

- [ ] **Step 5: Typecheck.**

```
cd /Users/r/playground/hail-website && pnpm tsc --noEmit -p tsconfig.json
```

Expected: clean. If TypeScript complains about the `jwks.keyPairConfig.alg` path (Better Auth's option shape may differ), correct it now using the discovered path from Step 2.

- [ ] **Step 6: Commit** (no app boot test yet — DB is missing the new tables).

```
git add hail-website/package.json hail-website/pnpm-lock.yaml hail-website/lib/auth.ts hail-website/.env.example
git commit -m "$(printf 'feat(auth): add jwt + oauth-provider + mcp Better Auth plugins\n\nWires the three plugins into lib/auth.ts with snake_case schema aliases\nfor the five new tables (jwks, oauth_clients, oauth_access_tokens,\noauth_refresh_tokens, oauth_consents). JWT plugin uses Better Auths\ndefault EdDSA / Ed25519 algorithm; the Python API was aligned to EdDSA\nin a follow-up to Phase 1a. DCR with unauthenticated registration\nenabled (for MCP agents); PKCE mandatory by default; TTLs 30d access /\n180d refresh per spec decision #6. Migration generated in the next\ncommit.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: Generate and apply the migration

**Files:**

- Create: `hail-website/better-auth_migrations/<timestamp>.sql`

- [ ] **Step 1: Discover the migration command.**

Better Auth exposes a CLI for schema generation. Run:

```
cd /Users/r/playground/hail-website && pnpm dlx @better-auth/cli --help
```

Expected: a help summary listing `generate` (and/or `migrate`). Use `generate` to write the SQL to a file; do **not** use `migrate` (which would apply it directly to the running DB — we want the SQL committed).

- [ ] **Step 2: Generate the migration.**

```
cd /Users/r/playground/hail-website && pnpm dlx @better-auth/cli generate --output "better-auth_migrations/$(date -u +%Y-%m-%dT%H-%M-%S.000Z).sql"
```

Expected: a new SQL file is written under `better-auth_migrations/` with `CREATE TABLE` statements for `jwks`, `oauth_clients`, `oauth_access_tokens`, `oauth_refresh_tokens`, and `oauth_consents`. **Review the file** — confirm column names are snake_case (matching the schema aliases from Task 1 Step 3).

- [ ] **Step 3: Apply the migration to local Postgres.**

```
cd /Users/r/playground/hail-website && psql "$DATABASE_URL" -f better-auth_migrations/<the-just-generated-file>.sql
```

Replace `<the-just-generated-file>` with the actual filename. Expected: `CREATE TABLE` × 5; no errors. If `DATABASE_URL` isn't set in your shell, source `.env.local` first.

- [ ] **Step 4: Verify the tables exist.**

```
cd /Users/r/playground/hail-website && psql "$DATABASE_URL" -c "\dt jwks oauth_clients oauth_access_tokens oauth_refresh_tokens oauth_consents"
```

Expected: five rows in the output.

- [ ] **Step 5: Boot the dev server.**

```
cd /Users/r/playground/hail-website && pnpm dev
```

Expected: no errors during startup. The Better Auth handler initializes; OAuth routes should be mounted (visible in the dev log if Better Auth logs them) — `Ctrl+C` to stop after confirming.

- [ ] **Step 6: Commit.**

```
git add hail-website/better-auth_migrations
git commit -m "$(printf 'feat(auth): add migration for oauth-provider + jwt tables\n\nCreates jwks, oauth_clients, oauth_access_tokens, oauth_refresh_tokens,\noauth_consents (all snake_case, matching the schema aliases from the\nprevious commit). Generated via @better-auth/cli; applied to local\nPostgres and verified.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: Consent page

**Files:**

- Create: `hail-website/app/consent/page.tsx`
- Create: `hail-website/app/consent/actions.ts`

The OAuth authorization flow redirects authenticated users to `/consent` with `client_id` + `scopes` query params; the page must let them allow/deny.

**Before writing any code in this task**, consult `hail-website/node_modules/next/dist/docs/` for the Next.js 16 conventions on server components, server actions, and how query params are read in a server component. AGENTS.md is explicit that this codebase's Next.js is not what training data suggests.

- [ ] **Step 1: Discover the Better Auth consent-handling API.**

Inspect `node_modules/better-auth/dist/plugins/oauth-provider` (or the installed `@better-auth/oauth-provider` package) to find the server-side helper that records a consent decision. Plausibly `auth.api.oauthConsent({ body: { allow: true } })` or similar — confirm the exact call signature and the expected redirect target. Record what you find in a comment at the top of `actions.ts`.

- [ ] **Step 2: Implement `actions.ts`.**

Create `hail-website/app/consent/actions.ts`:

```ts
"use server";

// The Better Auth oauth-provider plugin owns the redirect target after
// consent is recorded; we call its server-side helper rather than building
// the redirect URL by hand. Helper name confirmed against
// node_modules/@better-auth/oauth-provider in Task 3 Step 1.

import { auth } from "@/lib/auth";
import { headers } from "next/headers";
import { redirect } from "next/navigation";

export async function acceptConsentAction(consentId: string, allow: boolean) {
  // Replace the call below with the exact API surface discovered in Step 1.
  // The shape is documented to take a consent id and an allow boolean and
  // return the redirect URL the browser should navigate to next.
  const result = await auth.api.acceptOAuthConsent({
    headers: await headers(),
    body: { consentId, allow },
  });

  if (
    result &&
    typeof result === "object" &&
    "redirect" in result &&
    typeof result.redirect === "string"
  ) {
    redirect(result.redirect);
  }

  // Fallback: if the plugin's response shape changed, send the user back to
  // the console so they aren't stranded.
  redirect("/console");
}
```

If the helper name discovered in Step 1 differs from `acceptOAuthConsent`, adjust both the call and the type narrowing accordingly.

- [ ] **Step 3: Implement `page.tsx`.**

Create `hail-website/app/consent/page.tsx`. The Next.js 16 conventions for reading search params and rendering a server component are documented in `node_modules/next/dist/docs/` — consult those before writing the signature. As a starting point that matches Next.js 16's async-searchParams shape:

```tsx
import { auth } from "@/lib/auth";
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { acceptConsentAction } from "./actions";

export default async function ConsentPage({
  searchParams,
}: {
  searchParams: Promise<{
    consent_id?: string;
    client_id?: string;
    scope?: string;
  }>;
}) {
  const {
    consent_id: consentId,
    client_id: clientId,
    scope,
  } = await searchParams;
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session) redirect(`/signin?next=/consent`);
  if (!consentId || !clientId) redirect("/console");

  const scopes = (scope ?? "").split(" ").filter(Boolean);

  return (
    <main className="mx-auto max-w-md p-8">
      <h1 className="text-2xl font-bold mb-4">Authorize access</h1>
      <p className="mb-2">
        <strong>{clientId}</strong> wants to access your Hail account.
      </p>
      {scopes.length > 0 && (
        <ul className="mb-4 list-disc pl-6">
          {scopes.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
      <form className="flex gap-2">
        <button
          formAction={async () => {
            "use server";
            await acceptConsentAction(consentId, true);
          }}
          className="px-4 py-2 rounded bg-black text-white"
        >
          Allow
        </button>
        <button
          formAction={async () => {
            "use server";
            await acceptConsentAction(consentId, false);
          }}
          className="px-4 py-2 rounded border"
        >
          Deny
        </button>
      </form>
    </main>
  );
}
```

The styling is intentionally bare-minimum — visual polish is out of scope for Phase 1. If `console.css` (existing) provides utility classes used elsewhere in the console, prefer those over inline Tailwind.

- [ ] **Step 4: Typecheck.**

```
cd /Users/r/playground/hail-website && pnpm tsc --noEmit -p tsconfig.json
```

Expected: clean. Fix any errors arising from the helper-name guess (the exact shape of `auth.api.acceptOAuthConsent`'s body and response is what most likely needs a small adjustment).

- [ ] **Step 5: Smoke-test the page renders.**

Boot the dev server, visit `http://localhost:3000/consent?consent_id=test&client_id=test-client&scope=openid+profile` while signed in. Expected: the consent page renders with the Allow/Deny buttons and the scope list. Clicking either button will fail in this test (no real consent row exists) — that's fine; we only need to confirm the route is reachable.

- [ ] **Step 6: Commit.**

```
git add hail-website/app/consent
git commit -m "$(printf 'feat(auth): add OAuth consent page\n\nMinimal server-component consent screen for the oauth-provider authorization\nflow. Reads consent_id/client_id/scope from search params; renders Allow\nand Deny buttons that invoke a server action calling Better Auths\nconsent helper. Visual polish out of scope for Phase 1.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: Discovery + JWT round-trip test

**Files:**

- Create: `hail-website/tests/oauth-discovery.test.ts`

Verifies the discovery routes return well-formed metadata and that an issued JWT is verifiable against `/api/auth/jwks`. This is the **load-bearing** check that 1a + 1b are correctly aligned.

- [ ] **Step 1: Write the failing test.**

Create `hail-website/tests/oauth-discovery.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { auth, baseUrl } from "@/lib/auth";

// Discovery routes are auto-mounted by oauth-provider + mcp.
const AS_METADATA_PATH = "/api/auth/.well-known/oauth-authorization-server";
const PROTECTED_RESOURCE_METADATA_PATH =
  "/api/auth/.well-known/oauth-protected-resource";
const JWKS_PATH = "/api/auth/jwks";

async function fetchJson(path: string): Promise<unknown> {
  // Better Auth handlers are wired through Next.js; in vitest we hit the
  // server via the dev server URL embedded in baseUrl. The vitest config
  // expects a running dev server (or a fetch mock). If a test infra
  // helper exists in the repo for hitting Next.js routes from vitest,
  // use that here in place of raw fetch.
  const resp = await fetch(`${baseUrl}${path}`);
  if (!resp.ok) {
    throw new Error(`${path} → ${resp.status}`);
  }
  return await resp.json();
}

describe("OAuth discovery", () => {
  it("authorization-server metadata declares issuer, jwks_uri, supported algs", async () => {
    const meta = (await fetchJson(AS_METADATA_PATH)) as Record<string, unknown>;
    expect(meta.issuer).toBe(baseUrl);
    expect(meta.jwks_uri).toBe(`${baseUrl}${JWKS_PATH}`);
    expect(meta.id_token_signing_alg_values_supported).toContain("EdDSA");
    expect(meta.response_types_supported).toContain("code");
    expect(meta.code_challenge_methods_supported).toContain("S256");
  });

  it("protected-resource metadata points at this AS", async () => {
    const meta = (await fetchJson(PROTECTED_RESOURCE_METADATA_PATH)) as Record<
      string,
      unknown
    >;
    expect(meta.authorization_servers).toContain(baseUrl);
  });

  it("jwks endpoint returns EdDSA keys", async () => {
    const jwks = (await fetchJson(JWKS_PATH)) as {
      keys: Array<Record<string, unknown>>;
    };
    expect(jwks.keys.length).toBeGreaterThan(0);
    expect(jwks.keys.every((k) => k.alg === "EdDSA" || k.kty === "OKP")).toBe(
      true,
    );
  });
});
```

- [ ] **Step 2: Run the test against a running dev server.**

In one terminal: `cd /Users/r/playground/hail-website && pnpm dev`.
In another: `cd /Users/r/playground/hail-website && pnpm vitest run tests/oauth-discovery.test.ts`.
Expected: tests pass. If `jwks` returns a non-RS256 key, the algorithm config from Task 1 Step 3 didn't take effect — revisit and use the exact option path discovered in Task 1 Step 2.

If the test file's `fetch` against the dev server is too brittle for CI (which it may be — the dev server isn't always up), the implementer may instead use a different vitest strategy: import the route handler from `app/api/auth/[...all]/route.ts` and invoke it directly with a synthesized `Request` object. Either approach is acceptable; pick whichever the repo's existing test patterns favor (check `hail-website/tests/` and `app/api/internal/usage-events/rate/route.ts`'s tests if any).

- [ ] **Step 3: Commit.**

```
git add hail-website/tests/oauth-discovery.test.ts
git commit -m "$(printf 'test(auth): smoke-test OAuth discovery + JWKS\n\nGuards three load-bearing properties: AS metadata declares the right\nissuer/jwks_uri and advertises EdDSA + S256 PKCE; protected-resource\nmetadata points at this AS; /jwks returns Ed25519 keys. If the JWT\nalgorithm config drifts away from EdDSA, this test goes red.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 5: "Authorized apps" console panel

**Files:**

- Create: `hail-website/app/console/apps/page.tsx`
- Create: `hail-website/app/console/apps/AppsClient.tsx`
- Create: `hail-website/app/console/apps/actions.ts`

Mirrors the existing `app/console/keys/` pattern verbatim — page is a server component that fetches the org's authorized apps, AppsClient is a client component that renders the list with revoke buttons, actions.ts holds the server action.

- [ ] **Step 1: Read the existing `app/console/keys/` files.**

Read `app/console/keys/page.tsx`, `KeysClient.tsx`, and `actions.ts` to learn the repo's pattern (org resolution, list layout, revoke action, table styling, error toasts). The Apps panel reuses the same idioms.

- [ ] **Step 2: Implement `actions.ts`.**

Create `hail-website/app/console/apps/actions.ts`:

```ts
"use server";

import { pool } from "@/lib/db";
import { getCurrentSession, getActiveOrgIdForSession } from "@/lib/auth";

type AuthorizedApp = {
  clientId: string;
  name: string | null;
  uri: string | null;
  consentedAt: string;
  lastTokenIssuedAt: string | null;
  activeTokens: number;
};

export async function listAuthorizedAppsAction(): Promise<AuthorizedApp[]> {
  const session = await getCurrentSession();
  if (!session) return [];
  const orgId = await getActiveOrgIdForSession(session);
  if (!orgId) return [];

  // Apps are scoped via oauth_consents.user_id — the user authorized them.
  // We aggregate active access tokens (not yet expired or revoked) per
  // client so the panel can show "3 active tokens" alongside Revoke.
  const { rows } = await pool.query<AuthorizedApp>(
    `SELECT
       c.id AS "clientId",
       c.name,
       c.uri,
       cons.created_at AS "consentedAt",
       (SELECT max(t.created_at)::text FROM oauth_access_tokens t
          WHERE t.client_id = c.id AND t.user_id = $1) AS "lastTokenIssuedAt",
       (SELECT count(*)::int FROM oauth_access_tokens t
          WHERE t.client_id = c.id AND t.user_id = $1 AND t.expires_at > now()) AS "activeTokens"
     FROM oauth_consents cons
     JOIN oauth_clients c ON c.id = cons.client_id
     WHERE cons.user_id = $1
     ORDER BY cons.created_at DESC`,
    [session.user.id],
  );
  return rows;
}

export async function revokeAuthorizedAppAction(
  clientId: string,
): Promise<{ ok: true } | { ok: false; error: string }> {
  const session = await getCurrentSession();
  if (!session) return { ok: false, error: "not authenticated" };

  // Revocation = delete the consent + delete all access/refresh tokens for
  // this user×client pair. The OAuth provider plugin's POST /oauth2/revoke
  // is per-token; the console action covers the user-facing case "I no
  // longer want this app" by clearing both.
  const userId = session.user.id;
  await pool.query(
    `DELETE FROM oauth_access_tokens WHERE user_id = $1 AND client_id = $2`,
    [userId, clientId],
  );
  await pool.query(
    `DELETE FROM oauth_refresh_tokens WHERE user_id = $1 AND client_id = $2`,
    [userId, clientId],
  );
  await pool.query(
    `DELETE FROM oauth_consents WHERE user_id = $1 AND client_id = $2`,
    [userId, clientId],
  );
  return { ok: true };
}
```

- [ ] **Step 3: Implement `page.tsx`.**

```tsx
import { requireSession } from "@/lib/auth";
import { listAuthorizedAppsAction } from "./actions";
import { AppsClient } from "./AppsClient";

export default async function AuthorizedAppsPage() {
  await requireSession();
  const apps = await listAuthorizedAppsAction();
  return (
    <section className="console-section">
      <h2>Authorized apps</h2>
      <p className="muted">
        Applications that have OAuth access to your Hail account. Revoking
        removes all active tokens and the consent record; the next time the app
        connects you will be asked to authorize it again.
      </p>
      <AppsClient initialApps={apps} />
    </section>
  );
}
```

- [ ] **Step 4: Implement `AppsClient.tsx`.**

```tsx
"use client";

import { useState, useTransition } from "react";
import { revokeAuthorizedAppAction } from "./actions";

type App = {
  clientId: string;
  name: string | null;
  uri: string | null;
  consentedAt: string;
  lastTokenIssuedAt: string | null;
  activeTokens: number;
};

export function AppsClient({ initialApps }: { initialApps: App[] }) {
  const [apps, setApps] = useState(initialApps);
  const [pending, startTransition] = useTransition();

  function handleRevoke(clientId: string) {
    startTransition(async () => {
      const res = await revokeAuthorizedAppAction(clientId);
      if (res.ok) {
        setApps((cur) => cur.filter((a) => a.clientId !== clientId));
      } else {
        alert(`Could not revoke: ${res.error}`);
      }
    });
  }

  if (apps.length === 0) {
    return <p className="muted">You have not authorized any apps yet.</p>;
  }

  return (
    <table className="console-table">
      <thead>
        <tr>
          <th>App</th>
          <th>Authorized</th>
          <th>Last token</th>
          <th>Active tokens</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {apps.map((a) => (
          <tr key={a.clientId}>
            <td>
              {a.name ?? a.clientId}
              {a.uri && (
                <>
                  <br />
                  <a
                    className="muted"
                    href={a.uri}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {a.uri}
                  </a>
                </>
              )}
            </td>
            <td>{new Date(a.consentedAt).toLocaleDateString()}</td>
            <td>
              {a.lastTokenIssuedAt
                ? new Date(a.lastTokenIssuedAt).toLocaleDateString()
                : "—"}
            </td>
            <td>{a.activeTokens}</td>
            <td>
              <button
                type="button"
                disabled={pending}
                onClick={() => handleRevoke(a.clientId)}
              >
                Revoke
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

If the existing `KeysClient.tsx` has a confirmation modal pattern, mirror it here so an accidental revoke can be undone. If not, the inline `alert()` confirmation suffices for Phase 1.

- [ ] **Step 5: Add a console nav link.**

Locate the console nav (likely in `app/console/layout.tsx` or `app/console/OrgMenu.tsx`) and add a link to `/console/apps` next to the existing "Keys" link. Match the existing link styling.

- [ ] **Step 6: Typecheck.**

```
cd /Users/r/playground/hail-website && pnpm tsc --noEmit -p tsconfig.json
```

Expected: clean.

- [ ] **Step 7: Smoke test.**

Boot the dev server, sign in, visit `/console/apps`. Expected: empty-state copy ("You have not authorized any apps yet."). The full happy path (an authorized app appearing in the list) is exercised once an MCP client completes the OAuth flow in 1c — for Phase 1b we only verify the page renders correctly while the list is empty.

- [ ] **Step 8: Commit.**

```
git add hail-website/app/console/apps hail-website/app/console/layout.tsx hail-website/app/console/OrgMenu.tsx
git commit -m "$(printf 'feat(console): Authorized apps panel\n\nLists OAuth-authorized applications for the current user with a revoke\naction. Revoke deletes consent + all access/refresh tokens for that\nuser/client pair so the app must re-authorize the next time it\nconnects. Empty state until an MCP client completes OAuth in Phase 1c.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 6: Manual end-to-end smoke test

**Files:** none (manual verification).

The automated test in Task 4 covers discovery + JWKS. The full DCR + authorize + token + JWT-against-API path requires a real MCP client, which lands in 1c. For Phase 1b, exercise the AS by hand against `curl` to catch any wiring bug before declaring done.

- [ ] **Step 1: Register a dynamic client via DCR.**

With the dev server running:

```
curl -s -X POST http://localhost:3000/api/auth/oauth2/register \
  -H "Content-Type: application/json" \
  -d '{"client_name":"smoke","redirect_uris":["http://localhost:9999/cb"]}' | jq .
```

Expected: a JSON response containing `client_id`, `client_id_issued_at`, and `redirect_uris`. Note the `client_id`.

- [ ] **Step 2: Begin an authorization-code flow.**

Open a browser to:

```
http://localhost:3000/api/auth/oauth2/authorize?response_type=code&client_id=<the-client-id>&redirect_uri=http://localhost:9999/cb&code_challenge=<S256-of-a-verifier>&code_challenge_method=S256&scope=openid+profile&state=smoke
```

Expected: signed-in users are redirected to `/consent` with the right query params; clicking **Allow** redirects to `http://localhost:9999/cb?code=…&state=smoke` (your terminal won't have anything listening on :9999 — that's fine; copy the `code` from the URL).

- [ ] **Step 3: Exchange the code for a JWT.**

```
curl -s -X POST http://localhost:3000/api/auth/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=<the-code>&redirect_uri=http://localhost:9999/cb&client_id=<the-client-id>&code_verifier=<the-verifier>" | jq .
```

Expected: a JSON response with `access_token` (a JWT — 3 dot-separated segments) and `refresh_token`. Decode `access_token` at <https://jwt.io> or with `jq -R 'split(".")[1] | @base64d | fromjson'` — confirm `iss` = your `baseUrl`, `aud` includes the MCP resource, `exp` is ~30 days out, and `alg` (header) is `RS256`.

- [ ] **Step 4: Verify the JWT against the API.**

Point the local API at the dev hail-website by setting in `api/.env.local`:

```
BETTER_AUTH_ISSUER=http://localhost:3000
BETTER_AUTH_JWKS_URL=http://localhost:3000/api/auth/jwks
ALLOWED_AUDIENCES=http://localhost:8080,http://localhost:8081
```

Run the API: `cd /Users/r/playground/hail/api && uv run uvicorn hailhq.api.main:app --reload --port 8080`. Then:

```
curl -s -H "Authorization: Bearer <the-access-token>" http://localhost:8080/whoami
```

Expected: the API returns the principal with the right `organization_id` (resolved via the `members` table from the JWT `sub`). If it 401s, the API's JWKS fetch or audience check needs investigating — the 1a tests cover the API side, so the issue is most likely an audience mismatch (the JWT's `aud` is not in `ALLOWED_AUDIENCES`).

`/whoami` isn't a real API route — the existing routes (`/v1/calls`, etc.) all require POST bodies and reach into the call/email subsystems. If a lightweight identity-echo endpoint isn't available, exercise `GET /v1/calls?limit=1` which will return either `200 {"items":[],...}` (real-org Principal that has no calls yet) or 401 (auth failed) — either resolves the smoke check unambiguously.

- [ ] **Step 5: Record the smoke-test outcome in a small note.**

If any step needed adjustment, capture the deviation in a brief note appended to this plan's "Self-Review" section below.

This task does not commit anything — it's a pure verification. The branch is finished when Tasks 1–5 are committed and Task 6's smoke flow succeeds.

---

## Self-Review

- **Spec coverage** (§"1b — `hail-website` becomes the Authorization Server"):
  - `oauth-provider`, `mcp`, `jwt` plugins → Task 1 Step 3.
  - DCR with `allowUnauthenticatedClientRegistration: true` → Task 1 Step 3.
  - PKCE mandatory → Better Auth default, advertised in metadata, verified by Task 4 (`code_challenge_methods_supported` includes `S256`).
  - Audience-bound (RFC 8707) → Task 1 Step 3 (`validAudiences` config + `mcp({ resource })`).
  - JWKS at `/api/auth/jwks` (the Better Auth default; spec said "/jwks" — same thing under the `/api/auth` mount) → Task 4 asserts this.
  - Discovery routes auto-mounted → Task 4 asserts both `.well-known` routes.
  - TTL targets (Decision #6: ≥30d access / ≥180d refresh, sliding renewal) → Task 1 Step 3 (`accessTokenExpiresIn: "30d"`, `refreshTokenExpiresIn: "180d"`); refresh-token rotation is on by default per the docs.
  - Consent UI off-the-shelf → Task 3 (minimal bare consent screen; the actual _flow_ is Better Auth's, the page is a thin wrapper).
  - Existing `apiKey` / `deviceAuthorization` / `bearer` / `organization` plugins untouched → Task 1 Step 3(d) preserves them.
  - "Authorized apps" console panel → Task 5.
- **Cross-cutting JWT algorithm** (EdDSA / Ed25519): no explicit override; relies on Better Auth's default. Task 4 guards it via `id_token_signing_alg_values_supported` + JWKS `alg`/`kty` assertions so any drift breaks the test.
- **Placeholder scan:** No `TBD` / `TODO`. The one "verify at write time" instruction — Task 3 Step 1 (exact consent helper name) — is a _read_ instruction backed by a precise package path to inspect (`@better-auth/oauth-provider`), not a deferred decision.
- **Type / name consistency:** `clientId` (camel) ↔ `client_id` (snake) is the alias pattern used throughout; the SQL columns in Task 5's queries match the aliases declared in Task 1. The Vitest test in Task 4 and the curl commands in Task 6 use the same JWKS path (`/api/auth/jwks`).
- **Known follow-ups (NOT in this plan):**
  - MCP forwarder + per-request `HailClient` (Phase 1 sub-project 1c — separate plan).
  - The hail-website client cards (`app/mcp/clients.ts`), `hail/docs/setup/mcp.md`, and the homepage `CodePanel.tsx` get a "now OAuth-capable" pass in the docs+website plan, run _after_ 1c so the documented flow actually exists.
  - Polish on the consent page (organisation name + logo, scope-friendly copy) — Phase 1 ships bare bones; visual pass deferred.
  - Confirmation modal on the Apps revoke button — deferred until the `KeysClient` pattern is reused.

## Review follow-ups (2026-06-01)

Code review of the implemented branch verified the security-critical paths
(JWT `iss` contract, consent HMAC integrity, PKCE, redirect_uri allowlist,
revoke IDOR) against Better Auth source — all correct. Two issues were fixed
in code (`safeHref` now strips embedded credentials; the Apps list query
groups by client only + `MAX(created_at)` so a raced duplicate consent row
can't render twice). The following are **documented decisions / deferrals**,
not code-fixed — revisit in 1c:

- **A `resource` param is required to receive a JWT** (RFC 8707). Without it
  the plugin mints an _opaque_ access token in a normal 200, which the Hail
  API rejects with a silent 401. Documented inline at the `oauthProvider`
  config in `hail-website/lib/auth.ts`. Conformant MCP clients send
  `resource`; the discovery that tells a non-MCP client what value to send is
  RFC 9728 protected-resource metadata — the _resource server's_ job (MCP /
  API), deferred to 1c. This is also why `mcp()` is not mounted (route
  collision + hardcoded RS256 against absent tables).
- **No automated round-trip token test.** The hail-website vitest harness is
  unit-only with no test DB, so minting a real token (register → session →
  consent → code → token) isn't feasible there. The load-bearing alignment is
  covered by the discovery test (metadata `issuer`/`jwks_uri` + EdDSA-only)
  plus source verification that the token `iss` and metadata `issuer` share
  one source. Add an end-to-end check in 1c where the MCP forwarder exercises
  a real token against the API; until then, Task 6's manual curl smoke is the
  gate.
- **Unauthenticated DCR has no row cap.** Better Auth's default rate limit
  (100 req/10s/IP, prod-only) throttles spam rate but leaves total
  `oauth_clients` growth unbounded. A `rateLimit.customRules` entry was
  _deliberately not added_: hail-website configures no shared rate-limit
  storage, so on Vercel's multi-instance serverless the in-memory limiter is
  per-instance and resets on cold start — a `customRules` rule would imply
  protection it does not deliver. Real fixes for 1c: DB-backed rate-limit
  storage (or `secondaryStorage`), and/or a periodic prune of clients with
  zero consents/tokens older than N days.
