from fastapi import FastAPI, HTTPException

app = FastAPI(title="Hail MCP", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "mcp-stub"}


@app.get("/sse")
def sse_placeholder() -> None:
    raise HTTPException(
        status_code=501,
        detail="Hail MCP server — M1 in progress. See docs/setup/mcp.md.",
    )
