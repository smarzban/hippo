# MCP Server Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Roadmap item 6 — expose Hippo's four retrieval tools (search / read_document / list_documents / grep) to MCP-speaking harnesses (Claude Code etc.) over streamable-HTTP mounted at `/mcp`, plus a `hippo mcp` stdio mode for local single-user. Token-authenticated, role-filtered. claude.ai web connectors deferred. Design: `../specs/2026-06-12-team-readiness-design.md` §6.

**Architecture:** A `mcp_server.py` exposes the tools as plain role-parameterized functions (unit-testable) plus a `build_mcp_server(store, require_auth)` that registers thin `FastMCP` tool wrappers. The HTTP mount is gated by a pure-ASGI bearer-token middleware that resolves the token → role and sets a `ContextVar` the tools read; unauthenticated requests get 401 before MCP sees them. stdio mode runs as the local owner (admin). No LLM is involved — MCP tools call `Storage` directly, so the whole feature stays zero-network in tests.

**Tech stack:** `mcp` 1.27 (already present; declare it). `FastMCP(stateless_http=True, json_response=True)`, `streamable_http_path="/"`, mounted in FastAPI with a shared session-manager lifespan.

**Hard rules:** tests zero-network; all SQL in storage.py; retrieval methods take keyword-only `role`; TDD.

---

### Task 1 — `mcp_server.py`: tools + builder + role contextvar
- Declare `mcp>=1.27.0` in `pyproject.toml`.
- `src/hippo/mcp_server.py`:
  - `_mcp_role: ContextVar[str | None]` (default None) — the per-request role.
  - Plain functions `mcp_search(store, role, query, top_k=8)`, `mcp_read_document(store, role, doc_id)`, `mcp_list_documents(store, role, query=None)`, `mcp_grep(store, role, pattern)` returning the same dict shapes as `agent.py`'s tools (path/title/section/text; clean text — MCP consumers manage their own prompt hygiene, so no untrusted-data wrapping). grep catches `ValueError` → `[{"error": ...}]`.
  - `build_mcp_server(store, *, require_auth) -> FastMCP`: `FastMCP("hippo", stateless_http=True, json_response=True)`, `mcp.settings.streamable_http_path = "/"`. A `_role()` helper: returns the contextvar value; if None, raise `PermissionError` when `require_auth` (HTTP — middleware always sets it; this is defense-in-depth), else `"admin"` (stdio local owner). Register four `@mcp.tool()` wrappers delegating to the plain functions with `_role()`.
- Tests (`tests/test_mcp_server.py`): set `_mcp_role` and call the plain functions against an rbac store (everyone + managers sources) — developer can't see manager docs via search/list/get/grep; manager/admin can. Assert `build_mcp_server(...).list_tools()` (via `asyncio.run`) registers exactly `{search, read_document, list_documents, grep}`.

### Task 2 — mount at `/mcp` with bearer-token auth
- Settings: `mcp_enabled: bool = True`.
- `api.py`: a pure-ASGI middleware `_McpBearerAuth(app, store, settings)` — extract `Authorization: Bearer`, `store.resolve_token` → email → role (apply `admin_email_list` bootstrap, matching `_user_for`); on missing/invalid → 401 JSON; else `_mcp_role.set(role)` around `await self.app(...)` (reset in `finally`). When `settings.mcp_enabled`: build `mcp = build_mcp_server(store, require_auth=True)`, define a lifespan running `mcp.session_manager.run()`, construct `FastAPI(title="Hippo", lifespan=lifespan)`, and `app.mount("/mcp", _McpBearerAuth(mcp.streamable_http_app(), store, settings))`. When disabled, today's behavior (no lifespan, no mount).
- Tests (`tests/test_api_auth.py`): `/mcp` POST without token → 401; bogus bearer → 401; valid token → status != 401 (auth passed; use `with TestClient(app) as c:` so the session-manager lifespan runs). Existing 161 tests must stay green (apps built without `with` don't run the lifespan; the mount doesn't affect other routes).

### Task 3 — `hippo mcp` stdio + docs + gate
- `cli.py`: `hippo mcp` command — `_mcp_role.set("admin")`, `build_mcp_server(store, require_auth=False).run(transport="stdio")`. Test: `build_mcp_server(store, require_auth=False)` constructs and lists the 4 tools (the stdio run itself blocks — not unit-tested).
- README: an "MCP server" section — `claude mcp add --transport http hippo https://<host>/mcp --header "Authorization: Bearer $(hippo token create you@org.com)"`; note Claude Desktop via `mcp-remote`; claude.ai deferred. `hippo mcp` for local stdio.
- CLAUDE.md: add `mcp_server.py` to the architecture block; note `/mcp` mount + token auth + role filtering; `hippo mcp`; bump test count; roadmap item 6 → built (completing the round).
- Gate: `uv run pytest`, `cd ui && npm run build`, `docker build` (mcp in image), eval 4/4 if Ollama up.

## Self-review notes
- MCP tools call Storage with the token's role → same retrieval-layer enforcement as chat/REST; a developer's Claude Code can't surface manager docs.
- Pure-ASGI middleware (not BaseHTTPMiddleware) so the role contextvar propagates into the tool task and unauthenticated requests 401 before MCP processing.
- `stateless_http=True` keeps each request self-contained (token on every request), matching the per-request auth model.
