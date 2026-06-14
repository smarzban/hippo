# API layer

`api/` is the FastAPI application. It was a single ~770-line `build_app`
god-function; it's now a package where `build_app` is a thin assembler and the
substance lives in focused modules.

## Package shape

| File | Responsibility |
|---|---|
| `app.py` | `build_app(settings, model_override, *, iap_verifier, code_exchanger, google_key_fetcher)` — the thin assembler; also `_McpBearerAuth`. |
| `context.py` | `AppContext` + `build_context(...)` — the dependency bundle (connection, store, config overlay, snapshots, agent cache, ingestor, MCP server/lifespan). |
| `auth.py` | `make_auth_deps(ctx)` (the request dependencies) + the authz helper functions. |
| `models.py` | Pydantic request schemas, `_safe_filename`, and constants (`MIN_PASSWORD_LEN`, `MAX_NAME_LEN`, `_EMAIL_RE`). |
| `routes_session.py` | oidc/password auth + first-run wizard (`/auth/*`, `/setup*`). |
| `routes_account.py` | `/health`, `/me` (+ PATCH), `/me/password`, `/users*`. |
| `routes_content.py` | `/chat`, `/ingest`, `/documents*`, `/folders*`. |
| `routes_admin.py` | `/config*`, `/tokens*`, `/settings/status`. |

**Public surface unchanged:** `from hippo.api import build_app` (and the test
helper `_safe_filename`).

## What `build_app` does (the assembler)

1. Configure logging idempotently (so logs survive a direct ASGI launch, not just
   `hippo serve`).
2. `ctx = build_context(...)` — everything the old function did before `app =
   FastAPI(...)`.
3. Create the FastAPI app (with the MCP lifespan if MCP is enabled);
   `app.state.store = ctx.store`.
4. Add `SessionMiddleware` **once**, when `settings.secret_key` is set, with
   `https_only = ctx.public_url.startswith("https")`.
5. `auth = make_auth_deps(ctx)`.
6. Register the four route modules.
7. Mount the MCP app at `/mcp` (if enabled), then serve the SPA catch-all
   **last** so real API routes win.

There is **no CORS middleware** by design — the UI is same-origin (proxied in
dev, co-served in prod), so the browser's same-origin policy is an extra defense
layer.

## `AppContext` and the live-overlay contract

`AppContext` carries the shared objects (`store`, `cfg`, `settings`, the agent
cache, the ingestor, the IAP verifier, the construction-time auth snapshots) so
the route modules receive them explicitly instead of closing over two dozen
locals. The critical property preserved through the refactor:

- **Live reads stay live.** Handlers that re-resolve effective config per request
  still call `ctx.cfg.get(...)` / `ctx.live_agent()` against the *same* `cfg` and
  agent-cache objects. `chat_model` is read live per `/chat`; `allowed_domain` is
  read live in role resolution, `create_user`, and the auth-switch validator.
- **Construction-time snapshots stay snapshots.** `auth_mode` and the
  oidc/iap/domain wiring used by the oidc routes were resolved once at build time
  in the original; they remain `AppContext` fields.

`ctx.live_agent()` rebuilds the pydantic-ai agent when the effective `chat_model`
changes, caching `{model, agent}` so an unchanged model reuses the cached agent.

## Auth dependencies (`auth.py`)

The audit's specific ask was to make the auth logic testable without standing up
the whole app. `make_auth_deps(ctx)` returns a namespace of FastAPI dependency
callables:

- **`verify_request`** — authenticates per mode: bearer token (every mode) →
  `none` (local owner) → `iap` (verify the assertion) → `password` (session
  `user_id`) → oidc (session `email`). Resolves the role via `user_for`.
- **`require_admin`** (rank ≥ 1) and **`require_owner`** (rank ≥ 2).

Plus plain functions (ctx-first, importable and unit-testable on their own):

- `email_to_role(ctx, email)` / `user_for(ctx, email)` — identity → role, using
  the live `allowed_domain`.
- `require_folder_tier(user, folder)` — the per-folder tier guard layered on top
  of the `require_admin` floor (so an admin can't manage an owner-tier folder).
- `require_within_roots(ctx, location)` — the `HIPPO_SOURCE_ROOTS` allowlist
  enforcement.
- `require_mode_prereqs(ctx, target, …)` and `validate_auth_switch(ctx, user,
  target, …)` — the secret/prereq checks and anti-lockout guard for switching
  auth modes.

A single `auth` namespace is shared by all route modules so FastAPI dedups the
identical `Depends` callables within a request.

## Endpoint inventory

Auth/setup (`routes_session`): `GET /auth/login`, `GET /auth/callback`,
`GET /auth/logout` (oidc); `POST /auth/login`, `POST /auth/logout` (password);
`GET /auth/config`; `GET /setup/status`; `POST /setup`.

Account (`routes_account`): `GET /health`; `GET /me`, `PATCH /me`;
`POST /me/password`; `GET /users`, `POST /users`, `PUT /users/{email}/role`,
`POST /users/{email}/password`.

Content (`routes_content`): `POST /chat`; `POST /ingest`; `GET /documents`,
`GET /documents/{id}`; `GET /folders`, `POST /folders`, `PATCH /folders/{id}`,
`DELETE /folders/{id}`, `POST /folders/{id}/resync`.

Admin (`routes_admin`): `GET /config`, `PUT /config`; `GET /tokens`,
`POST /tokens`, `DELETE /tokens/{id}`; `GET /settings/status`.

Authorization specifics (effective-role tier guards, anti-lockout, the
`/ingest` `versioned:false` back-compat field) are documented inline in the
route handlers and in [Auth & RBAC](auth-and-rbac.md).

## `/chat` streaming

`POST /chat` builds `HubDeps(store, role)` and streams via
`VercelAIAdapter.dispatch_request(request, agent=ctx.live_agent(), deps=...,
usage_limits=usage_limits(settings))`. The `usage_limits` cap (from
`HIPPO_MAX_TOOL_CALLS`) is what surfaces as the "research limit" message in the
UI. See [Agent](agent.md).
