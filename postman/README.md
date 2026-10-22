# Hail: calls, texts, and email for AI agents

Give your AI assistant a phone number and email. Get numbers, send messages,
answer calls and texts, and track delivery through one API.

[Create a Hail account](https://hail.so/signup) ·
[API documentation](https://hail.so/docs/api) ·
[Pricing](https://hail.so/pricing) ·
[Source](https://github.com/hail-hq/hail)

[![Run in Postman](https://run.pstmn.io/button.svg)](https://app.getpostman.com/run-collection/59134891-d34d3c4a-00eb-43f8-853d-8fc7859696b2?action=collection%2Ffork&source=rip_markdown&collection-url=entityId%3D59134891-d34d3c4a-00eb-43f8-853d-8fc7859696b2%26entityType%3Dcollection%26workspaceId%3Daeb5d784-d1b6-4e09-a7ad-0d2e5b442d08)

## Get started

1. Create a Hail account and get an API key from the console or `hail auth token`.
2. Fork the **Hail API** collection into your own workspace.
3. Click the collection → **Variables** tab. Paste your Hail key as the value of `api_key`.
   Do not click **Share** next to it: unshared values stay on your machine.
4. Open the **Start here** folder. Send **Check API health**, then **List my agents**. Neither spends credit.
5. Continue through the steps in order. Each step saves the IDs that the next steps use.

Hail uses prepaid credit, with no platform subscription. Add credit before
sending messages or making calls. Dedicated numbers have monthly rent and may
have a setup fee. Buying a number charges the setup fee and first month's rent.

**Start here** skips requests that spend credit unless `enable_paid_requests`
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

## What is in the collection

**Start here** is the first folder: six steps, from checking your key to
placing a call. It starts with read-only requests. It does not submit identity
documents or release numbers.

The other folders list every public API route, grouped by area, generated from
Hail's OpenAPI definition. They include the current request descriptions,
fields, and response examples. Edit the placeholder values before sending a
request. They include write and delete operations; use individual requests
rather than running them in full.

## Use MCP instead

Connect your MCP client to `https://mcp.hail.so` and authorize Hail in the browser.
See the [MCP setup guide](https://hail.so/mcp).
