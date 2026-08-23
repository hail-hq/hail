# API versioning

`/v1/<resource>` is the canonical, documented form of every customer-facing
Hail API route. It appears in the OpenAPI spec (`openapi/openapi.yaml`) and
is what the CLI and generated clients target.

## Legacy unprefixed paths

Routes without the `/v1` prefix (e.g. `/whoami` instead of `/v1/whoami`)
still work, for existing integrations built before versioning shipped. They
are not in the OpenAPI spec and should not be used for new integrations.

Every response from a legacy path carries:

- `Deprecation: true` — this path is deprecated (see the IETF Deprecation
  HTTP header field).
- `Link: </v1/...>; rel="successor-version"` — the canonical `/v1` path
  that replaces it, as a relative path (not an absolute URL).

## Sunset

No sunset date is set for the legacy paths yet. If one is scheduled, it
will be announced here and via a `Sunset` response header (RFC 8594) added
ahead of the change, giving integrators advance notice before the
unprefixed paths stop working.
