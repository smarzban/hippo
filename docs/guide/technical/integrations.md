# Integrations: MCP & Slack

Both surfaces reuse the same retrieval and the same role filtering as the web
chat. Neither has a privileged path into `Storage`.

## MCP server (`mcp_server.py`)

A [FastMCP](https://github.com/jlowin/fastmcp) server exposing the same four
tools — `search`, `read_document`, `list_documents`, `grep` — over MCP.

### Two run modes

- **Mounted at `/mcp`** on the FastAPI app (when `HIPPO_MCP_ENABLED`, default
  true). This is the multi-user remote mode; each request carries a bearer token.
- **Stdio** via `hippo mcp` — a local single-user server that runs **as owner**
  with no token (you're already on the box).

### Bearer auth + role propagation

The HTTP mount is wrapped by `_McpBearerAuth` (in `api/app.py`), a **pure-ASGI**
gate (not `BaseHTTPMiddleware`):

1. It reads the `Authorization: Bearer …` header, resolves the token to an email,
   then to a role via the shared `email_to_role` (which applies the domain gate).
2. If resolution fails, it rejects with 401 **before** any MCP processing.
3. On success it sets the role into the `_mcp_role` **contextvar**, runs the MCP
   app, and resets it in a `finally`.

Pure-ASGI is deliberate: it ensures the contextvar propagates into the MCP tool
task and that unauthenticated requests never reach MCP logic. The module-level
tool functions read `_mcp_role` to pass `role` into `Storage`, so MCP retrieval
is filtered exactly like chat (an `admin` token sees user+admin tiers; a `user`
token sees user only).

### Sync/async dispatch

FastMCP runs sync tools on the event-loop thread, so the tools are `async def`
and offload the blocking `Storage` calls via `anyio.to_thread.run_sync(...)`,
capturing the role from the contextvar at call time. This keeps the event loop
responsive and the shared SQLite connection serialized through `Storage`'s lock.

### The endpoint detail

It's served at `/mcp/`; a request to `/mcp` (no trailing slash) redirects there,
which MCP clients follow. See [Using MCP](../users/using-mcp.md) for client
setup.

## Slack bot (`slack_bot.py`)

A Slack Q&A bot over **Socket Mode** (`slack-bolt`) — an outbound WebSocket, so
no public endpoint and it works behind IAP. Runs as its own process: `hippo
slack` (refuses to start unless `HIPPO_SLACK_ENABLED=true`).

### Structure

The module is split into **pure helpers** + an adapter + wiring, so the logic is
testable with a fake client:

- `surface_role(...)`, `build_history(...)`, `format_answer(...)`,
  `answer_question(...)` — pure functions.
- `handle_event(...)` — the adapter (tested against a fake Slack client).
- `build_slack_app(...)` — the bolt wiring.

It uses the shared `resolve_role(..., allowed_domain=...)` (threaded through so it
respects the live overlay) and `usage_limits` (the same tool-call budget as
chat), and `safe_log` to sanitize any logged email.

### Surface-split access (important)

Access is tuned to **where** the question is asked:

- **DM** → the asker's full role (their tier).
- **Channel `@mention`** → **`user`-tier only**, regardless of the asker's actual
  role. Admin/owner content never surfaces in a shared channel.

This keeps higher-tier content out of channels by construction, not by
convention. History is thread-aware: DMs are a flowing conversation; in channels,
reply in-thread and `@mention` again to continue. See
[Using Slack](../users/using-slack.md).

## Why reuse, not reimplement

Both integrations call the same `Storage` + agent. There is intentionally no
second retrieval path — a new surface inherits grounding and access control for
free, and there's no place for a divergent filter to leak content.
