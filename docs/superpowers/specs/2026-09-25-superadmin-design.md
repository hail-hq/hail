# Superadmin role — design

Date: 2026-09-25. Status: approved in chat (owner: r13i).

## Goal

Hail staff (`*@hail.so`) can open any customer organization in the console, with owner powers, without being listed as a member. Customers see staff actions in their activity trail as "Hail support".

## Decisions

- Who: a user whose email ends with `@hail.so`, is verified, and whose last login was Google or email+password. GitHub logins never qualify (GitHub may carry an unverified address).
- Powers: everything an owner can do in the chosen org.
- Visibility: no `members` row is ever created. Audit rows carry `actor_kind = superadmin`; the console prints "Hail support" for them.
- Scope of the power: the console session only. API keys, the CLI and MCP tokens never carry the role. They keep acting on the person's own organization, as today.
- Not included: two-factor login. A stolen `@hail.so` Google account gives owner power on every org. Tracked as a follow-up.

## Why not member rows

The API resolves the organization for an API key, a CLI key and an MCP token from the user's first membership (lowest org id). A staff member with rows in every org would have those tools land in a random customer's org. Virtual membership avoids that entirely.

## Website (hail-website)

- `lib/superadmin.ts`: `isSuperadmin(user)` = `email.endsWith("@hail.so") && emailVerified && lastLoginMethod in {google, email}`. `lastLoginMethod` comes from Better Auth's `lastLoginMethod` plugin stored on the user (`users.last_login_method`).
- `getCurrentSession()` returns `session.superadmin` (computed, cached per request).
- `getActiveOrgIdForSession`: unchanged for members. For a superadmin, `sessions.active_organization_id` may point to any org.
- `requireOrgAdmin` and `getOrgMemberRole`: a superadmin is treated as `owner`.
- Org switcher: `app/console/OrgSwitcher.tsx`, rendered in the top bar for superadmins only. Search by org name, owner email or org id (server action `searchOrgsAction`, superadmin-gated, max 20 rows). Picking one runs `switchOrgAction(orgId)`: superadmin check, org exists, `UPDATE sessions SET active_organization_id`. A banner "Viewing <org name> as Hail support · back to my org" shows while the active org is one the user is not a member of.
- JWT `definePayload`: adds `superadmin: true` when the session is a superadmin's. Never for other users.
- `lib/require-superadmin.ts` (from #94): `isSuperadmin()` reads the session flag.
- Members settings page: nothing to hide; a superadmin is not in the list. Ownership transfer and member management stay member-only actions in the UI, but the superadmin passes the role check like an owner.

## API (hail-hq/hail)

- `Principal` gains `superadmin: bool = False`.
- JWT path (`deps.py`): if claim `superadmin` is `true`, the `activeOrganizationId` org must exist (`organizations` row) but no membership is required. Without the claim, behaviour is unchanged. The claim is trusted because the token is signed by the website with the EdDSA key the API already verifies.
- `require_superadmin`: `principal.auth_kind == "jwt" and principal.superadmin`, else 403.
- Contacts role check (`_member_role`): a superadmin counts as `owner`.
- Audit log: migration `0047_audit_actor` adds `actor_user_id uuid null` and `actor_kind text null` (`api_key`, `user`, `superadmin`, `system`). `write_audit_log` takes the principal (or explicit actor) and fills both. Existing callers pass the principal.
- `/whoami` returns `superadmin`.
- Rate limit: unchanged (per credential). Funds gate: unchanged (org balance).

## Tests

- Website: superadmin detection (domain, verification, login method), role bypass, switcher gate (non-superadmin gets 404/403), JWT claim present only for superadmins, banner rendering.
- API: JWT with `superadmin` and a foreign org resolves; without the claim 403 as before; API key on `/admin/verifications` 403; audit row carries `actor_kind = superadmin`.

## Rollout

1. hail (API) PR first: accepts the claim, ignores it when absent. Safe to deploy alone.
2. hail-website PR second: detection, switcher, claim.
3. Prod: no new env vars.
