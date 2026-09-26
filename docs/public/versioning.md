# API versioning

```bash
# Legacy path: the response carries the deprecation headers
curl -si -H "Authorization: Bearer $HAIL_API_KEY" "$HAIL_API_URL/whoami" | grep -i -E '^(HTTP|deprecation|link)'
# HTTP/1.1 200 OK
# deprecation: true
# link: </v1/whoami>; rel="successor-version"

# Canonical path: no deprecation headers
curl -si -H "Authorization: Bearer $HAIL_API_KEY" "$HAIL_API_URL/v1/whoami" | grep -i -E '^(HTTP|deprecation|link)'
# HTTP/1.1 200 OK
```

`/v1/<resource>` is the canonical, documented form of every customer-facing
Hail API route. It appears in the OpenAPI spec
([`openapi/openapi.yaml`](https://github.com/hail-hq/hail/blob/main/openapi/openapi.yaml))
and is what the CLI and generated clients target.

## Legacy unprefixed paths

Routes without the `/v1` prefix (e.g. `/whoami` instead of `/v1/whoami`)
still work, for existing integrations built before versioning shipped. They
are not in the OpenAPI spec and should not be used for new integrations.

Every response from a legacy customer route carries the headers below, set by
[`api/hailhq/api/deprecation.py`](https://github.com/hail-hq/hail/blob/main/api/hailhq/api/deprecation.py).
The one exception is a `429` returned by the general rate limiter, which is
produced before that step runs.

- `Deprecation: true` — this path is deprecated (see the IETF Deprecation
  HTTP header field).
- `Link: </v1/...>; rel="successor-version"` — the canonical `/v1` path
  that replaces it, as a relative path (not an absolute URL).

Paths that have no `/v1` twin (`/healthz`, `/openapi.json`, `/docs`,
`/redoc`) never carry these headers.

## Sunset

No sunset date is set for the legacy paths yet. If one is scheduled, it
will be announced here and via a `Sunset` response header (RFC 8594) added
ahead of the change, giving integrators advance notice before the
unprefixed paths stop working.

Provider-facing routes are the exception: Hail still emits `/unsubscribe`
links in sent emails and registers `/sms/status` and `/sms/telnyx` callbacks
with the SMS providers on the unprefixed path. Those must keep working, or be
migrated first, before any sunset.
