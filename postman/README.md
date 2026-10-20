# Hail: calls, texts, and email for AI agents

Give your AI assistant a phone number and email. Get numbers, send messages,
answer calls and texts, and track delivery through one API.

[Create a Hail account](https://hail.so/signup) ·
[API documentation](https://hail.so/docs/api) ·
[Pricing](https://hail.so/pricing) ·
[Source](https://github.com/hail-hq/hail)

## Get started

1. Create a Hail account and get an API key from the console or `hail auth token`.
2. Fork the **Hail API: Start here** collection into your own workspace.
3. Add your Hail key to your local Postman Vault as `hail-api-key`.
   The collection's `api_key` variable contains the reference `{{vault:hail-api-key}}`.
   Keep that reference when sharing the collection. Your key stays in your vault.
4. Send **Check API health**, then **List my agents**. Neither spends credit.
5. Use **Get number quotes** to see live offers. Fill in the request bodies for the actions you want to try.

Hail uses prepaid credit, with no platform subscription. Add credit before
sending messages or making calls. Dedicated numbers have monthly rent and may
have a setup fee. Buying a number charges the setup fee and first month's rent.

The quickstart skips requests that spend credit unless `enable_paid_requests`
is `true`. Turn it on only when you are ready to send those requests.
Use your own test phone and email, and send only with the recipient's consent.

## What you can try

- Find numbers by country, city, region, or area code.
- Get a number and route it to an assistant.
- Set instructions, language, greeting, and human handover contacts.
- Read verification requirements and check submissions.
- Send email from a Hail address or your verified domain.
- Read received email and track delivery events.
- Send texts, make calls, and read their results.

Calls and texts share history for the same agent and caller. Email is available
through the API and MCP, but is not yet part of that built-in shared history.
Custom domains need DNS verification. Some numbers need carrier approval, and
US long-code texting needs an approved SMS registration.

## Collections

**Hail API: Start here** has a short set of examples with editable variables.
It starts with read-only requests. It does not submit identity documents or
release numbers.

**Hail API Reference** is generated from Hail's OpenAPI definition. It includes
the current request descriptions, fields, and response examples. Edit the
placeholder values before sending a request. The reference includes write
and delete operations; use individual requests rather than running it in full.

## Use MCP instead

Connect your MCP client to `https://mcp.hail.so` and authorize Hail in the browser.
See the [MCP setup guide](https://hail.so/mcp).

## Keep this workspace current

The OpenAPI specification in Hail's source repository drives the API Reference.
The repository's Postman workflow generates both collections, checks them,
and can update the published collections after changes reach `main`.

This overview is also used as both collections' introduction. Changes to it
reach their documentation on the next sync. Postman's separate workspace
overview must be updated manually from this file.
