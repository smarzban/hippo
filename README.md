# Hippo

Agentic org knowledge base: feed it markdown/text/HTML/`.docx`/Google-Docs-exports, ask it questions
in chat — it answers **only** from the indexed docs, with citations. Role-governed (three-tier RBAC:
user/admin/owner), four auth modes, a browser setup wizard, MCP + Slack surfaces; self-hosted in a
single binary — and still runs fine for one person.
Spec (historical v1): `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` · Decisions: `...-decisions.md`

**Supported upload/ingest formats:** `.md`, `.txt`, `.html`, `.docx` (Word / Google-Docs "Download as .docx"). Download a Google Doc as `.docx` and upload it — headings are preserved. PDF and direct Google-Drive links are not yet supported (planned).

## Quickstart

    uv sync
    cp .env.example .env                    # then edit it — see the two options below

**Option A — OpenAI (default).** In `.env`, set a real key:

    OPENAI_API_KEY=sk-...                   # chat + embeddings use OpenAI defaults

**Option B — local Ollama.** Point at Ollama's OpenAI-compatible API and use a local
embedding model (Ollama Cloud does not serve embeddings). In `.env`:

    OPENAI_API_KEY=ollama
    OPENAI_BASE_URL=http://localhost:11434/v1
    HIPPO_CHAT_MODEL=openai:gpt-oss:120b-cloud
    HIPPO_ENRICH_MODEL=openai:gpt-oss:120b-cloud
    HIPPO_EMBEDDING_MODEL=nomic-embed-text
    HIPPO_EMBEDDING_DIM=768

`ollama pull nomic-embed-text`; cloud models need `ollama signin`.

Then — `OPENAI_*` vars must be in the process environment, so load `.env` before starting:

    set -a; source .env; set +a            # OPENAI_* are not auto-loaded from .env
    uv run hippo sync eval/fixtures         # ingest the bundled sample docs (or your own folder)
    uv run hippo serve                      # API on :8000
    cd ui && npm install && npm run dev     # chat UI on :5173 (proxies to :8000)

`.env.example` lists every configurable setting; the table below documents the common ones.

## Configuration (env, prefix HIPPO_)

| Var | Default | Notes |
|---|---|---|
| `HIPPO_DB_PATH` | `hippo.db` | the whole brain is this file |
| `HIPPO_CHAT_MODEL` | `openai:gpt-5.2` | any pydantic-ai model string, e.g. `anthropic:claude-opus-4-8` |
| `HIPPO_EMBEDDING_MODEL` | `text-embedding-3-small` | `fake` = offline deterministic (dev/tests) |
| `HIPPO_EMBEDDING_DIM` | `1536` | must match the model; run `hippo reindex` after changing |
| `HIPPO_ENRICH_ENABLED` | `true` | contextual lines + summaries at ingestion (cheap model) |
| `HIPPO_ENRICH_MODEL` | `openai:gpt-5-mini` | |
| `HIPPO_AUTH_MODE` | `none` | `none` \| `oidc` \| `iap` \| `password` — see Authentication below |
| `HIPPO_ALLOWED_DOMAIN` | _(unset)_ | restrict sign-in to this Google Workspace domain (e.g. `example.com`) |
| `HIPPO_ADMIN_EMAILS` | _(unset)_ | comma-separated emails that get admin role on first sign-in |
| `HIPPO_SECRET_KEY` | _(required for oidc/password)_ | random secret for session cookie signing |
| `HIPPO_OIDC_CLIENT_ID` | _(required for oidc)_ | Google OAuth2 client ID |
| `HIPPO_OIDC_CLIENT_SECRET` | _(required for oidc)_ | Google OAuth2 client secret |
| `HIPPO_PUBLIC_URL` | _(required for oidc)_ | public base URL, e.g. `https://hippo.example.com` (used for OAuth redirect URI) |
| `HIPPO_IAP_AUDIENCE` | _(required for iap)_ | GCP IAP backend service audience (`/projects/…/…`) |
| `HIPPO_SOURCE_ROOTS` | _(unset)_ | colon-separated allowed ingest paths; required in `oidc`/`iap` modes |
| `HIPPO_MAX_UPLOAD_BYTES` | `10485760` | reject multipart uploads larger than this (413) |
| `HIPPO_MAX_DOC_CHARS` | `1000000` | skip docs exceeding this char count before embedding (status: `skipped`) |
| `HIPPO_UI_DIST` | _(unset)_ | path to built UI (`ui/dist`) for FastAPI to serve on one origin; set automatically in the Docker image |
| `HIPPO_SLACK_ENABLED` | `false` | enable the `hippo slack` bot |
| `HIPPO_SLACK_BOT_TOKEN` | _(unset)_ | Slack bot token (`xoxb-…`) |
| `HIPPO_SLACK_APP_TOKEN` | _(unset)_ | Slack app-level token (`xapp-…`, Socket Mode) |

## Authentication

Hippo supports four auth modes, set via `HIPPO_AUTH_MODE`:

- **`none`** (default) — no authentication; every request is treated as a local owner. Suitable for personal use or private networks only (`serve` prints a warning if bound beyond localhost). Note: in `none` mode the API is open **including during the first-run window**, so for a network-reachable deployment either start in `oidc`/`iap` (gated by the IdP even before setup) or keep the instance private until the wizard switches it to `password` mode.
- **`oidc`** — in-app Google sign-in. Users are redirected to `/auth/login`, authenticate with Google, and receive a session cookie. Requires `HIPPO_OIDC_CLIENT_ID`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_SECRET_KEY`, and `HIPPO_PUBLIC_URL`. Optionally restrict to a single Google Workspace domain with `HIPPO_ALLOWED_DOMAIN`.
- **`iap`** — deployed behind [GCP Identity-Aware Proxy](https://cloud.google.com/iap). Hippo verifies the `x-goog-iap-jwt-assertion` header on every request. Requires `HIPPO_IAP_AUDIENCE`.
- **`password`** — built-in email + password login. Users sign in with their email and a password that is argon2id-hashed in the database. Accounts are locked for 15 minutes after 5 consecutive failures. Sessions are 7-day signed cookies. Requires `HIPPO_SECRET_KEY`; no `HIPPO_OIDC_*` settings are needed. **There are no default credentials** — the first owner is created via the first-run wizard (see below) or the break-glass CLI.

**Bearer tokens** are accepted in every mode for headless clients (Slack bot, MCP server, CI scripts). Create a token with `hippo token create <email>`.

**Password mode bootstrap (break-glass).** The normal first-run flow is the browser wizard (see below). If you need to create or reset credentials from the CLI:

    hippo user set-password owner@example.com --role owner

The command prompts for the password twice (no echo). Re-run it at any time to reset a forgotten password or unlock a locked-out account; the `--role` option is only applied when creating a new user (existing users keep their current role unless `--role` is given).

**Password mode UI.** When `auth_mode=password`, the React SPA shows a login form (email + password) instead of the Google button. Signed-in users can change their own password from the Settings → My Profile tab (self-service: requires the current password). Admins can reset any lower-tier user's password from the Users tab; the new password is displayed once and must be copied immediately.

**Roles:** users have one of three roles — `user` (default), `admin`, or `owner`. Set roles with `hippo role set <email> <role>`. Content is tiered by the folder it lives in — a `user`-tier folder is visible to everyone; an `admin`-tier folder is visible to `admin` and `owner`; an `owner`-tier folder is visible only to `owner`. Admins can manage folders and tokens via the API or the Settings UI. Emails listed in `HIPPO_ADMIN_EMAILS` are always promoted to `owner` on sign-in.

## First-run wizard

When Hippo starts with an empty database it enters **setup mode**. Open the browser — instead of the chat UI you'll see a single-page setup form with these fields:

- **Setup token** — enter the setup token. Set `HIPPO_SETUP_TOKEN` in the environment before starting; if unset, a one-time random token is printed to the startup logs (grep for `first-run setup token is:`). The server validates the token first, so a wrong token is rejected immediately (403) rather than at the end.
- **Auth mode** — choose `password`, `oidc`, or `iap` (`none` stays a dev-only env setting, not offered in the wizard).
- **Owner account** — enter the owner email. For `password` mode, also set the initial password (8 characters minimum, validated inline). For `oidc`/`iap`, provide the email that will be the owner on first sign-in.
- **Models** (optional) — override `chat_model` (and `enrich_model`). Leave blank to use the env/`.env` defaults. (`embedding_model`/`embedding_dim` are env-only — set them with `HIPPO_EMBEDDING_*` before first ingest; the wizard does not change them.)

Submitting posts to `POST /setup`, which creates the owner, persists the chosen operational config, marks setup complete, and reloads the app into the normal chat view. The single-page form does not send folder names, so the three default root folders keep their seeded names (`Default`/`Private`/`Owner`) — rename them later in **Settings → Folders**. (The `POST /setup` endpoint itself still accepts an optional `roots` rename for API callers; the wizard simply no longer uses it.)

The setup endpoint is gated by the token and refuses to run again once setup is complete (409). The wizard is the recommended path for team deployments.

## Config store

Hippo keeps a `config` table in the database for operational, **non-secret** settings. Owners can change these live via the browser (System config tab) or `PUT /config`. The DB value wins over the env default for these keys:

| Key | Notes |
|---|---|
| `auth_mode` | takes effect on next `hippo serve` restart |
| `chat_model` | live per-request — no restart needed |
| `enrich_model` | takes effect on next restart |
| `allowed_domain` | takes effect on next restart |
| `oidc_client_id` / `public_url` / `iap_audience` | takes effect on next restart |

**Secrets are always env-only.** `OPENAI_API_KEY`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_SECRET_KEY`, `HIPPO_SETUP_TOKEN`, and all other credentials are never stored in the database and never returned by any API endpoint.

**`embedding_model` / `embedding_dim` are env-only**, not DB-overridable. The vector space and the `chunk_vec` table width are fixed when the index is created and only change via `hippo reindex` (a CLI op that reads the environment). A DB override could neither take effect nor stay accurate after a reindex, so the env-built embedder is the single source of truth — set them with `HIPPO_EMBEDDING_MODEL` / `HIPPO_EMBEDDING_DIM` and run `hippo reindex` to change them.

## Settings UI

Every signed-in user can access the Settings view via the gear (⚙) button in the header. From there:

- **My Profile** (everyone) — view your email (read-only login identity) and edit your display name; change your own password (password mode); and create, list, and revoke your own personal access tokens (`hk_…`). The plaintext token secret is shown exactly once after creation. Use these tokens for MCP clients, the Slack bot, and CI scripts. Each token carries your own role (no escalation).
- **Folders** (admin only) — browse the folder tree, create child folders, rename/delete folders, or trigger a re-sync on filesystem-synced folders. Each folder has a tier (`user`, `admin`, or `owner`) inherited from its parent. Documents live in exactly one folder; upload access is gated by the folder's tier.
- **Users** (admin only) — list all users, create a new user (with an optional one-time password in password mode), and change a user's role. An admin cannot demote their own account (anti-lockout guard) and cannot grant or reset a role above their own tier.
- **Status** (admin only) — read-only view of the instance configuration: effective auth mode and models (from the DB overlay if set), setup status, repo wiring, MCP/Slack status, and doc/folder/user counts. No secrets are exposed.
- **System config** (owner only) — live-edit operational settings stored in the config table. `chat_model` and `enrich_model` can be changed any time; `embedding_model`/`embedding_dim` are read-only once documents exist (change them via `hippo reindex`); `auth_mode` has an anti-lockout guard (you must hold a valid credential in the target mode before switching).

**Uploading documents:** click "Add doc" in the header, pick a file, and select one or more destination folders from the modal. Only folders writable by your role are shown (manual folders at or below your tier). The same file can be ingested into multiple folders.

API endpoints backing the SPA and headless clients: `GET /health`, `GET /me`, `PATCH /me`, `GET /auth/config`, `POST /auth/login`, `POST /auth/logout`, `POST /me/password`, `GET /documents`, `GET /documents/{id}`, `GET /users`, `POST /users` (admin create-user), `PUT /users/{email}/role`, `POST /users/{email}/password` (admin reset), `GET /tokens`, `POST /tokens`, `DELETE /tokens/{id}`, `GET /folders`, `POST /folders`, `PATCH /folders/{id}`, `DELETE /folders/{id}`, `POST /folders/{id}/resync`, `GET /settings/status`, `GET /setup/status`, `POST /setup`, `GET /config`, `PUT /config`.

**Legacy database note:** SP1 (roles & folder model) introduced a new database schema with no migration. A pre-SP1 database (with `documents.source_id` and no `folders` table) is rejected on startup with a clear "recreate the database" error. Delete the old `.db` file and re-sync your content.

## CLI

    hippo sync [FOLDER] [--watch]         # register+sync folder / re-sync all synced folders
    hippo add FILE                        # ingest one file
    hippo search QUERY                    # debug hybrid search
    hippo reindex                         # re-embed after model change
    hippo eval eval/golden.yaml           # retrieval recall@k
    hippo serve                           # FastAPI server
    hippo role set EMAIL ROLE             # set user role (user|admin|owner)
    hippo role list                       # list all users and their roles
    hippo token create EMAIL              # create a bearer token for headless access
    hippo token list EMAIL                # list a user's tokens (never the secret)
    hippo token revoke EMAIL ID           # revoke a token by id
    hippo user set-password EMAIL [--role ROLE]   # set/reset a local password (password mode bootstrap)
    hippo mcp                             # MCP server over stdio (local single-user, owner)
    hippo slack                           # Slack bot over Socket Mode (read-only Q&A)

## Running with Docker

    docker compose up --build

`.env` must exist with at least `OPENAI_API_KEY` (or remove the `env_file` line from `compose.yaml` if you wire env vars another way). For a host Ollama instance set `OPENAI_BASE_URL=http://host.docker.internal:11434/v1`. The image is multi-stage; the final stage serves both the API and the built UI on a single origin at `:8000`.

## Backups

    hippo backup snapshot.db

Writes a consistent single-file snapshot via SQLite `VACUUM INTO`. Safe regardless of WAL state — no need to pause writes or copy WAL files separately.

## MCP server

Hippo exposes its search/read/list/grep tools over MCP so Claude Code (and other harnesses) can query the knowledge base, role-filtered by the caller's token.

**Remote (multi-user):** run `hippo serve`; each user creates a token and adds the server:

```bash
hippo token create you@org.com          # prints hk_...
claude mcp add --transport http hippo https://hippo.example.com/mcp \
  --header "Authorization: Bearer hk_..."
```

The endpoint is served at `/mcp/`; a request to `/mcp` (no trailing slash) is redirected there, which MCP clients follow automatically.

Claude Desktop users: use [`mcp-remote`](https://github.com/geelen/mcp-remote) to inject the header. claude.ai web connectors are not yet supported (need MCP OAuth — planned).

**Local single-user:** `hippo mcp` runs an MCP server over stdio (as owner — no token needed). Point a local stdio MCP client at it:

```bash
uv run hippo mcp
```

Example `.claude/mcp.json` entry:

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

**Role filtering:** an `admin` token sees user- and admin-tier folders; a `user` token sees only user-tier folders — enforced in Storage, the same as chat. `HIPPO_MCP_ENABLED=false` disables the `/mcp` HTTP mount.

## Slack bot

Ask Hippo questions from Slack — DM the app, or `@Hippo <question>` in a channel.
Answers are role-filtered: a DM uses your full access; a channel @mention only ever
surfaces `user`-tier docs (admin/owner-tier content stays in DMs). Follow-ups work —
DMs are a flowing conversation; in channels, reply in the thread and `@Hippo` again.

It runs in **Socket Mode** (an outbound WebSocket), so it needs no public endpoint and
works behind IAP. Create a Slack app with Socket Mode enabled, the bot scopes
`app_mentions:read, chat:write, im:history, im:read, im:write, users:read,
users:read.email, channels:history, groups:history`, and event subscriptions
`app_mention` + `message.im`. Then:

```bash
export HIPPO_SLACK_ENABLED=true
export HIPPO_SLACK_BOT_TOKEN=xoxb-…     # Bot User OAuth Token
export HIPPO_SLACK_APP_TOKEN=xapp-…     # App-Level Token (connections:write)
uv run hippo slack
```

Run it as its own process/container alongside `hippo serve` (it keeps its own
connection to the same DB).

## Tests

    uv run pytest                 # no network: fake embedder + TestModel/FunctionModel
