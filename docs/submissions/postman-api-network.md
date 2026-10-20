---
target: "Postman API Network"
slug: postman-api-network
category: dev-directory
url: https://www.postman.com/explore
score: 9
status: drafted
---

# Postman API Network

## TODO

- [x] Confirm public workspace and collection requirements.
- [x] Prepare workspace copy and a signup link.
- [x] Generate a reference collection from the current OpenAPI specification.
- [x] Prepare a short collection for trying the API with a user's own key.
- [x] Implement generation, validation, and optional collection updates in CI.
- [ ] Owner creates a Postman account and Hail team.
- [ ] Import and try both collections in Postman.
- [ ] Make the Hail workspace public.
- [ ] Record the public workspace URL here.
- [ ] Enable automatic updates after reviewing the published collections.

## Steps to submit

1. Create an account at <https://www.postman.com/> and a team named **Hail**.
   Choose a Hail-related public handle if it is available. The user confirmed
   on 2026-10-20 that no Postman account exists yet.
2. Create a workspace named **Hail API**. Use an internal workspace while
   preparing it, then change its visibility to public when ready.
3. Add the summary and overview from **Content** below. Use the Hail logo at
   `hail-website/public/assets/monogram-512.png` for the team avatar.
4. Generate the collections from the `hail` repository:

   ```sh
   npm ci --prefix scripts/postman
   npm test --prefix scripts/postman
   npm run generate --prefix scripts/postman
   ```

5. In Postman, choose **Import**, then import these files:
   - `postman/hail-start-here.postman_collection.json`
   - `postman/hail-api-reference.postman_collection.json`
6. Fork **Hail API: Start here** to a separate test workspace. Store your Hail
   API key in your local Postman Vault as `hail-api-key`. Send **Check API
   health**, then **List my agents**. No credit is spent by those requests.
   Review the individual examples you want to try before enabling paid requests.
7. Return to the original workspace. Its collection variables must contain
   vault references and placeholders, never your API key or customer data.
8. Open **Workspace overview → Settings**, change the workspace type to
   **Public**, and save. A team approval request may be needed depending on
   the account plan and role. Once public, the workspace and collections are
   listed on the Postman API Network. This is not a separate directory form.
9. Record the public workspace URL and each collection's UID in **Notes**.
10. To keep both collections current, create a Postman API key with access to
    the two collections. This is a **Postman key**, not a Hail API key. In the
    Hail GitHub repository, configure:
    - Secret `POSTMAN_API_KEY`.
    - Variable `POSTMAN_REFERENCE_COLLECTION_UID`.
    - Variable `POSTMAN_QUICKSTART_COLLECTION_UID`.
11. After approving the sync setup, set repository variable
    `POSTMAN_AUTO_SYNC` to `true`. Future relevant changes on `main` then
    update the two existing collections. To test publication manually, run
    **Postman collections** on `main` with **publish** selected. Neither
    mechanism creates a team or changes workspace visibility.

The workflow generates downloadable import files on pull requests and on
relevant pushes even when publication is disabled. The collection overview,
API fields, descriptions, and examples update from repository sources.
The separate workspace overview requires manual changes; copy it again from
`postman/README.md` when the product introduction changes.

## Content

### Team

Name: **Hail**

Website: <https://hail.so>

### Workspace

Name: **Hail API**

Summary:

> Try calls, texts, email, phone numbers, and AI assistants with your own Hail API key.

Overview: copy [postman/README.md](../../postman/README.md). Both generated
collections include the same introduction and signup link.

Collections:

- **Hail API: Start here**: a short, editable introduction to number quotes,
  agents, routing, verification status, domains, email delivery, calls, and SMS.
- **Hail API Reference**: all public operations generated from
  [openapi/openapi.yaml](../../openapi/openapi.yaml), grouped by API tag.

### Optional MCP collection

After the API workspace is live, add a native MCP request in Postman:

1. Choose **New → MCP** and select **HTTP**.
2. Use `https://mcp.hail.so` and name it **Hail Cloud MCP**.
3. Use Postman's OAuth flow to authorize Hail. Verify the flow with the new
   account before publishing; it has not been tested in this environment.
4. Load capabilities and try `list_agents` or `list_numbers` first.
5. Save the request to a new collection named **Hail MCP**. Use the setup
   text at <https://hail.so/docs/mcp> as its description source.

Native MCP requests use Postman's multi-protocol collection format. The
generated v2.1 HTTP files are not presented as native MCP collections.
Inclusion in Postman's curated MCP Catalog is a separate review: after the
MCP collection is public, request review at `api-network@postman.com`.
No email has been sent.

## Notes

- Status: prepared locally; not imported or published.
- Account/team: not created.
- Workspace URL: pending.
- Reference collection UID: pending.
- Quickstart collection UID: pending.
- Auto-sync: disabled until configured. No GitHub secrets or variables changed.
- Local validation: both files pass the official Postman v2.1 JSON schema;
  5 tests pass for current API fields, stable request IDs, paid-request opt-in,
  and publication target/error handling. The public API health request returns
  HTTP 200. The pinned dependency audit reports no vulnerabilities.
- Postman import/runtime results: pending account creation. Local validation
  does not replace this check. No paid Hail requests have been sent.
- Updates replace the contents of these two collections from repository
  sources. Make edits here, or use a fork for custom examples.
- Existing request and response IDs are preserved when updating matching
  operations, so unchanged request links can survive a sync.

Sources checked on 2026-10-20:

- [FlashDocs example](https://www.postman.com/flashdocs/flashdocs/overview).
- [Publish public APIs](https://learning.postman.com/docs/postman-api-network/showcase/publish/overview/).
- [Import an API specification](https://learning.postman.com/docs/design-apis/specifications/import-a-specification/).
- [Maintain public collections](https://learning.postman.com/docs/postman-api-network/showcase/maintain-public-apis).
- [Replace a collection](https://learning.postman.com/api-docs/api-reference/collections/put-collection).
- [Use vault secrets](https://learning.postman.com/docs/use/postman-vault/use-vault-secrets).
- [Publisher verification](https://learning.postman.com/docs/postman-api-network/showcase/prepare/verify-publisher-team).
- [Create an MCP request](https://learning.postman.com/docs/use/send-requests/protocols/mcp-requests/create).
- [MCP Catalog review](https://learning.postman.com/docs/postman-api-network/showcase/publish/mcp-servers/promote/).
