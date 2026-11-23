"""Serve a setup guide when someone opens the MCP URL in a browser."""

from html import escape

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER


class SetupPageMiddleware:
    def __init__(self, app: ASGIApp, endpoint_url: str, guide_url: str) -> None:
        self.app = app
        self.page = HTMLResponse(
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect to Hail MCP</title>
  <style>
    body {{ font: 1rem/1.6 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }}
    code {{ overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <main>
    <h1>Connect to Hail MCP</h1>
    <ol>
      <li>In your MCP client, add a remote server with URL <code>{escape(endpoint_url)}</code>.</li>
      <li>Sign in to Hail when your client asks you to authorize the connection.</li>
    </ol>
    <p><a href="{escape(guide_url)}">Find setup instructions for your MCP client</a>.</p>
  </main>
</body>
</html>
""",
            headers={"Cache-Control": "no-store"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"] == "/"
            and scope["method"] in {"GET", "HEAD"}
        ):
            headers = Headers(scope=scope)
            # Streamable HTTP GET requests accept SSE. Session and protocol
            # headers also identify MCP clients using this same URL.
            accept = {
                value.partition(";")[0].strip().lower()
                for header in headers.getlist("accept")
                for value in header.split(",")
            }
            if (
                "authorization" not in headers
                and MCP_SESSION_ID_HEADER not in headers
                and MCP_PROTOCOL_VERSION_HEADER not in headers
                and "text/event-stream" not in accept
                and (not accept or accept == {"*/*"} or "text/html" in accept)
            ):
                response: Response = self.page
                if scope["method"] == "HEAD":
                    response = Response(headers=self.page.headers)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
