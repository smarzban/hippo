# Hippo Settings UI — Design (2026-06-13)

Roadmap item **9**. A Settings surface in the React app over the existing admin/role/token
machinery — most of which is currently **CLI-only**. Brainstormed and approved with Saeed on
2026-06-13. Builds on the auth/roles work (PR #3) and reuses `/me` for role-gating.

---

## 1. Scope

Four panels in a Settings view: **Sources**, **Users & Roles**, **Tokens**, **Status**.

- **Sources** (admin) — list / add (from `HIPPO_SOURCE_ROOTS`) / set access (`everyone`↔`managers`)
  / delete / **re-sync**.
- **Users & Roles** (admin) — list users, change role (`developer`/`manager`/`admin`).
- **Tokens** (self-service, everyone) — create / list / revoke your **own** personal access
  tokens; admins can additionally see and revoke **anyone's**.
- **Status** (admin) — read-only view of how the instance is wired.

**Out of scope (YAGNI):** editing models/auth/config from the UI (status is read-only), audit
logs, bulk ops, user invites / SSO provisioning (users appear on first login), token expiry.

## 2. Access model — split

- **Sources, Users & Roles, Status** → **admin-only**, enforced by the existing `require_admin`
  dependency; the client also hides these tabs for non-admins (convenience, not the boundary).
- **Tokens** → **self-service for every signed-in user**, behind `verify_request` and
  **scoped server-side to the caller's own email**. A non-admin can only ever read/create/revoke
  their own tokens; creating a token can't escalate privileges (it inherits the creator's role).
  Admins get an `?all=true` view and can revoke any token.

Rejected: *fully admin-only* (an admin must mint every developer's MCP token — constant
friction, no security benefit since self-minted tokens don't escalate).

## 3. Backend — endpoints

Sources endpoints already exist (`GET/POST /sources`, `DELETE /sources/{id}`). New:

| Method & path | Auth | Backed by | Notes |
|---|---|---|---|
| `POST /sources/{id}/resync` | admin | `list_sources`→location, then `sync_folder` | Re-ingest an existing source; awaits and returns the same `{report}` shape as `POST /sources`. 404 if id unknown. |
| `GET /users` | admin | `Storage.list_users()` | `[{"email","role"}]` |
| `PUT /users/{email}/role` | admin | `Storage.set_role` | body `{"role": "developer"|"manager"|"admin"}`; 400 on invalid role; **400 if the caller targets their own account with a non-admin role** (anti-lockout); note: emails in `HIPPO_ADMIN_EMAILS` are always admin via `resolve_role` regardless of stored role. |
| `GET /tokens` | self (`?all=true`→admin) | `list_tokens(email)` / new `list_all_tokens()` | metadata only: `[{"id","name","created_at","last_used_at"[,"email"]}]` — never the secret. |
| `POST /tokens` | self | `create_token(caller_email, name)` | body `{"name": str}`; returns `{"id","token":"hk_…"}` — the plaintext **once**. |
| `DELETE /tokens/{id}` | self / admin | `revoke_token(id, caller_email)` else new `revoke_token_any(id)` | 404 if not found / not yours (non-admin). |
| `GET /settings/status` | admin | settings + counts | `{auth_mode, chat_model, embedding_model, repos:{team:bool,managers:bool}, mcp_enabled, slack_enabled, counts:{documents,sources,users}}`. **Bools only — never secrets/tokens.** |

**New `Storage` methods (SQL stays in `storage.py`):**
- `list_all_tokens() -> list[tuple[int,str,str,str,str|None]]` — `(id, email, name, created_at, last_used_at)` across all users (admin view).
- `revoke_token_any(token_id: int) -> bool` — delete by id without the email scope (admin revoke).

The existing `revoke_token(token_id, email)` (already email-scoped) remains the self-service path.

**SPA catch-all:** add `"users"`, `"tokens"`, `"settings"` to the `RESERVED` prefix tuple in
`api.py` so the production single-origin catch-all doesn't shadow the new API routes.

## 4. Frontend

The app is a single chat page (`ui/src/App.tsx`, no router) that already fetches `/me`
(`{email, role, auth_mode}`). Add:

- A **gear button** in the header → toggles a `view` state (`"chat"` ↔ `"settings"`). No router
  dependency (YAGNI for two views); back-to-chat button returns.
- A **`Settings` component** (`ui/src/Settings.tsx`) with **tabs** rendered by role: admins see
  Sources · Users · Tokens · Status; managers/developers see only **Tokens**.
- Data via `fetch` to the endpoints above (same-origin; dev proxy must learn the new paths).
- **Token-secret-once UX:** after `POST /tokens`, show the `hk_…` once with a copy button and a
  "you won't see this again" note; the list view (from `GET /tokens`) shows metadata only.
- Small, framework-light: pull any non-trivial logic into pure helpers (e.g. role→tabs,
  response shaping) so it's unit-testable; the React rendering is covered by the `npm run build`
  gate, matching the current UI's posture (no component test harness today).
- **Vite dev proxy** (`ui/vite.config.ts`): add `/users`, `/tokens`, `/settings` to the proxied
  paths alongside `/chat,/ingest,/documents,/sources`.

## 5. Testing (zero-network, as always)

`tests/test_api_settings.py` via `TestClient`, matching `test_api_auth.py`'s role/token helpers:
- admin can `GET /users` and `PUT` a role; a developer gets **403** on `/users` and `/settings/status`.
- a developer can `POST`/`GET`/`DELETE` **their own** tokens; `POST /tokens` returns the secret
  once, then `GET /tokens` shows metadata only (no secret field).
- a developer **cannot** revoke another user's token (404); an admin can (`revoke_token_any`).
- `?all=true` is admin-only (developer → 403) and returns owners' emails.
- anti-lockout: an admin `PUT`ing their own account to `developer` → 400.
- `POST /sources/{id}/resync` returns a report for a known id, 404 for unknown.
- `GET /settings/status` exposes bools/counts and **no secret values**.
- new `Storage` methods (`list_all_tokens`, `revoke_token_any`) unit-tested in `tests/test_storage*`.

No SQL outside `storage.py`. Retrieval keeps keyword-only `role`. TDD.

## 6. Decisions / rejected alternatives

- **No router** — two views; a state toggle is simpler and avoids `react-router-dom` + touching
  the SPA catch-all routing. Revisit if the UI grows more pages.
- **Status is read-only** — editing models/auth from the UI means writing env/secrets at runtime;
  out of scope and risky. The strip is for visibility only.
- **Anti-lockout via self-demotion guard** (not "last admin" detection) — simpler, covers the
  common foot-gun; `HIPPO_ADMIN_EMAILS` is the ultimate recovery hatch regardless.
- **Self-service tokens can't escalate** — `create_token` ties the token to the caller's email
  and therefore their role; this is what makes opening the Tokens panel to everyone safe.
