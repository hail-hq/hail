# Investigate MCP authentication rejections

Use the request trace in Logfire, then compare a later authenticated tool call.
A 401 count alone does not establish that a customer could not connect.

The discovery middleware adds bounded attributes to the existing HTTP span:

| Attribute                   | Meaning                                                                                 |
| --------------------------- | --------------------------------------------------------------------------------------- |
| `mcp.request.method`        | Known protocol method, `other`, `unknown`, or `uninspected`                             |
| `mcp.auth.header_present`   | Whether the original request carried an Authorization header                            |
| `mcp.auth.credential_kind`  | Shape of the Authorization header: `none`, `bearer_jwt`, `bearer_other`, `other_scheme` |
| `mcp.auth.discovery`        | Whether the existing anonymous-discovery rule supplied its synthetic bearer             |
| `mcp.auth.rejection_reason` | Local decision category, set only when the response is 401                              |

Rejection categories:

- `missing_authorization`: a non-POST request carried no credentials.
- `protected_method`: an anonymous POST named a method outside public discovery.
- `missing_session`: an older-protocol anonymous discovery request had no session header.
- `discovery_rate_limited`: anonymous initialize exceeded the existing per-IP cap.
- `body_too_large`, `body_timeout`, `invalid_request`: the bounded discovery peek could not accept the request.
- `credentials_rejected`: credentials were forwarded, but downstream returned 401. This does not identify a JWT failure reason. Group by `mcp.auth.credential_kind` to tell a bad JWT from a bad API key or a wrong scheme.
- `discovery_rejected`: a request allowed by the anonymous-discovery rule still returned 401 downstream.

Authenticated bodies are not inspected. Only known protocol method names are
exported; unknown method strings are replaced with `other`. Tokens, session IDs,
RPC arguments, request bodies and additional personal identifiers are not logged.
Authentication, discovery rules, body limits and responses are unchanged.

```sql
SELECT attributes->>'mcp.auth.rejection_reason' AS reason,
       attributes->>'mcp.auth.credential_kind' AS credential,
       attributes->>'mcp.request.method' AS method,
       COALESCE(attributes->>'user_agent.original',
                attributes->>'http.user_agent') AS client,
       count(*) AS requests
FROM records
WHERE service_name = 'hail-mcp'
  AND COALESCE(attributes->>'http.response.status_code',
               attributes->>'http.status_code') = '401'
  AND start_timestamp >= 'START_UTC'
  AND start_timestamp < 'END_UTC'
GROUP BY 1, 2, 3, 4 ORDER BY requests DESC;
```

When using the Logfire CLI, pass both `--start` and `--end`; its hosted default is
30 minutes even if SQL requests a wider window. Missing diagnostic fields can
mean an older deployment. Do not infer a rejection reason from missing fields.
