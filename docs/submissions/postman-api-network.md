---
target: "Postman API Network"
slug: postman-api-network
category: dev-directory
url: https://www.postman.com/explore
score: 9
status: submitted
---

# Postman API Network

## TODO

- [x] Confirm public workspace and collection requirements.
- [x] Prepare workspace copy and a signup link.
- [x] Generate one collection from the current OpenAPI specification, with a
      **Start here** folder for trying the API with a user's own key.
- [x] Implement generation, validation, and optional collection updates in CI.
- [x] Owner creates a Postman account and Hail team.
- [x] Import and try the collection in Postman.
- [x] Make the Hail workspace public.
- [x] Record the public workspace URL here.
- [x] Enable automatic updates after reviewing the published collection.
- [x] Pin **Hail API** under **Featured collections** on the workspace **Docs** tab.

## Steps to submit

1. Create an account at <https://www.postman.com/> and a team named **Hail**.
   Make the team profile public: profile in the header → **Hail** →
   **Team profile** → **Make my team's profile public**. Then **Edit Your
   Public Profile**: add the team description, website `https://hail.so`, and
   the logo from `hail-website/public/assets/monogram-512.png`
   (**Upload profile photo**).
2. Click **Workspaces** in the header → **Create**. Name it **Hail API**,
   choose **Internal** and **Blank workspace**, then **Create Workspace**.
   The create form has no summary or description fields.
3. In the workspace, open **Overview → Docs**. Set the summary and the
   description from **Content** below. Changes save automatically.
4. Generate the collection from the `hail` repository:

   ```sh
   pnpm install
   pnpm postman:test
   pnpm postman:generate
   ```

5. In Postman, choose **Import**, then import
   `postman/hail-api.postman_collection.json`. Keep its name **Hail API**:
   automatic updates refuse a collection with another name.
6. Fork **Hail API** to a separate test workspace. In the fork, open the
   **Variables** tab and paste your Hail API key as the value of `api_key`.
   Do not click **Share**. In **Start here**, send **Check API health**, then
   **List my agents**. Both must return 200. No credit is spent.
   Review the individual examples you want to try before enabling paid requests.
7. Return to the original workspace. Its `api_key` variable must be empty,
   never your API key.
8. In the sidebar, click **⋯ → Workspace overview → Settings**. Under
   **Workspace type**, click **Change**, select **Public**, then **Save
   changes** (or **Submit request** if approval is needed). Once public, the
   workspace and collection are listed on the Postman API Network. This is
   not a separate directory form.
9. Record the public workspace URL and the collection UID in **Notes**.
10. To keep the collection current, create a Postman API key with access to
    it. This is a **Postman key**, not a Hail API key. In the
    Hail GitHub repository, configure:
    - Secret `POSTMAN_API_KEY`.
    - Variable `POSTMAN_COLLECTION_UID`.
11. After approving the sync setup, set repository variable
    `POSTMAN_AUTO_SYNC` to `true`. Future relevant changes on `main` then
    update the existing collection. To test publication manually, run
    **Postman collections** on `main` with **publish** selected. Neither
    mechanism creates a team or changes workspace visibility.

The workflow generates a downloadable import file on pull requests and on
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

Description: copy [postman/README.md](../../postman/README.md). The generated
collection includes the same introduction and signup link.

Collection **Hail API**:

- **Start here** folder: six steps through number quotes, agents, routing,
  verification status, domains, email delivery, calls, and SMS. Each step
  saves the IDs the next steps use.
- One folder per API tag: all public operations generated from
  [openapi/openapi.yaml](../../openapi/openapi.yaml).

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

- Status: public on 2026-10-22.
- Team profile: <https://www.postman.com/hail-hq>.
- Workspace URL: <https://www.postman.com/hail-hq/hail-api>.
- Collection UID: stored in the repository variable `POSTMAN_COLLECTION_UID`.
- Auto-sync: on (`POSTMAN_AUTO_SYNC=true`). The first manual publish run
  succeeded on 2026-10-22.
- Signed-out visitors see a lock next to the collection: they can read and
  fork it, not edit it.
- Local validation: the file passes the official Postman v2.1 JSON schema;
  6 tests pass for current API fields, stable request IDs, saved IDs between
  steps, paid-request opt-in, and publication target/error handling. The public API health request returns
  HTTP 200.
- Postman runtime: a `{{vault:...}}` reference inside a plain collection
  variable returned 401, so the collection does not use the vault. Users
  paste their key into `api_key`.
  No paid Hail requests have been sent.
- Updates replace the contents of this collection from repository
  sources. Make edits here, or use a fork for custom examples.
- Existing request and response IDs are preserved when updating matching
  operations, so unchanged request links can survive a sync.

Sources checked on 2026-10-20:

- [FlashDocs example](https://www.postman.com/flashdocs/flashdocs/overview).
- [Publish public APIs](https://learning.postman.com/docs/postman-api-network/showcase/publish/overview/).
- [Import an API specification](https://learning.postman.com/docs/design-apis/specifications/import-a-specification/).
- [Maintain public collections](https://learning.postman.com/docs/postman-api-network/showcase/maintain-public-apis).
- [Replace a collection](https://learning.postman.com/api-docs/api-reference/collections/put-collection).
- [Public workspaces](https://learning.postman.com/docs/collaborating-in-postman/using-workspaces/public-workspaces/).
- [Edit workspace details](https://learning.postman.com/docs/collaborating-in-postman/using-workspaces/internal-workspaces/manage-workspaces/#edit-workspace-details).
- [Share variables](https://learning.postman.com/docs/use/send-requests/variables/share-variables/).
- [Publisher verification](https://learning.postman.com/docs/postman-api-network/showcase/prepare/verify-publisher-team).
- [Create an MCP request](https://learning.postman.com/docs/use/send-requests/protocols/mcp-requests/create).
- [MCP Catalog review](https://learning.postman.com/docs/postman-api-network/showcase/publish/mcp-servers/promote/).
