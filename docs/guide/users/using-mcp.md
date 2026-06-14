# Using Hippo from Claude (MCP)

Hippo exposes its search/read/list/grep tools over the **Model Context Protocol
(MCP)**, so Claude Code and other MCP harnesses can query your knowledge base
directly. Access is **role-filtered by your bearer token** — an MCP client sees
exactly what you'd see in chat.

## Remote (multi-user) — recommended for teams

Run `hippo serve`; the MCP server is mounted at `/mcp` on the same origin. Each
person uses their own token:

```bash
hippo token create you@org.com          # prints hk_...  (or make one in Settings → My Profile)

claude mcp add --transport http hippo https://hippo.example.com/mcp \
  --header "Authorization: Bearer hk_..."
```

The endpoint is served at `/mcp/`; a request to `/mcp` (no trailing slash)
redirects there, which MCP clients follow automatically.

## Claude Desktop

Claude Desktop's native connector can't attach a static bearer header (it expects
OAuth), so use [`mcp-remote`](https://github.com/geelen/mcp-remote) to inject the
header. (Web claude.ai connectors aren't supported yet — that needs MCP OAuth,
which is planned.)

## Local single-user

`hippo mcp` runs an MCP server over **stdio**, as owner, with no token needed —
handy on your own machine:

```bash
uv run hippo mcp
```

Example `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "hippo": {
      "command": "uv",
      "args": ["run", "hippo", "mcp"],
      "cwd": "/path/to/hippo"
    }
  }
}
```

## What the tools do

The MCP server offers the same four tools the chat agent uses:

- **search** — hybrid keyword + semantic search over your documents.
- **read_document** — fetch a full document by id.
- **list_documents** — list available documents (optionally filtered).
- **grep** — exact/regex scan over document text.

## Role filtering

An `admin` token sees `user`- and `admin`-tier folders; a `user` token sees only
`user`-tier — enforced in Hippo's storage layer, the same filter as chat. There's
no way to see more through MCP than your role allows.

## Disabling MCP

Operators can turn the HTTP mount off with `HIPPO_MCP_ENABLED=false`.

For how this is implemented (bearer gate, role propagation), see the
[Integrations internals](../technical/integrations.md).
