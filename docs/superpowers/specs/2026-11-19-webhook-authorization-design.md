# Webhook receiver authorization

Academate's Cursor endpoint rejects unsigned receiver access with HTTP 401.
Hail already signs payloads, but Cursor also requires its own Authorization
header. Add an optional, write-only `authorization` field to subscriptions.
Encrypt it with the existing webhook SecretCipher and return only
`has_authorization`. Existing subscriptions continue without a header.

Keep omitted or null values unchanged on PATCH; an empty string removes the
header. Changing the target URL while retaining a saved credential requires
an explicit replacement or removal, to avoid disclosing it to another receiver.
Reject control characters and oversized values without echoing credentials.
Continue signing the payload, never follow redirects, and scrub credentials
from stored receiver responses and exceptions.

The console uses a password input and shows whether a header is saved, with
replace and remove actions. Saving clears the typed value. No automatic
resume or replay occurs. Backend migration/API deploy must precede the console.

## Approaches

A dedicated Authorization field handles Cursor and other bearer/basic receivers
with a small, clear interface. Arbitrary headers add validation and editing
work unrelated to this failure. A relay introduces another service and secret
store. Use the dedicated field in the existing subscription table.

## Validation

Prove encrypted storage, response privacy, omission/removal, URL-change
protection, worker Authorization plus HMAC, and scrubbed delivery diagnostics.
Test the console form, build it, and review screenshots in both themes at
1500px, 390px, and 320px. Test delivery against a local authenticated receiver;
live Cursor testing requires the owner's key and authorization to replay.
