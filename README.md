# Hippo

Agentic team brain: feed it markdown/text/HTML/`.docx`/Google-Docs-exports, ask it questions in chat.
Spec: `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` · Decisions: `...-decisions.md`

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
| `HIPPO_AUTH_MODE` | `none` | `none` \| `oidc` \| `iap` — see Authentication below |
| `HIPPO_ALLOWED_DOMAIN` | _(unset)_ | restrict sign-in to this Google Workspace domain (e.g. `example.com`) |
| `HIPPO_ADMIN_EMAILS` | _(unset)_ | comma-separated emails that get admin role on first sign-in |
| `HIPPO_SECRET_KEY` | _(required for oidc)_ | random secret for session cookie signing |
| `HIPPO_OIDC_CLIENT_ID` | _(required for oidc)_ | Google OAuth2 client ID |
| `HIPPO_OIDC_CLIENT_SECRET` | _(required for oidc)_ | Google OAuth2 client secret |
| `HIPPO_PUBLIC_URL` | _(required for oidc)_ | public base URL, e.g. `https://hippo.example.com` (used for OAuth redirect URI) |
| `HIPPO_IAP_AUDIENCE` | _(required for iap)_ | GCP IAP backend service audience (`/projects/…/…`) |
| `HIPPO_SOURCE_ROOTS` | _(unset)_ | colon-separated allowed ingest paths; required in `oidc`/`iap` modes |
| `HIPPO_GITHUB_TOKEN` | _(unset)_ | GitHub personal access token for upload-to-repo |
| `HIPPO_GITHUB_DOCS_REPO` | _(unset)_ | `owner/repo` for developer doc uploads |
| `HIPPO_GITHUB_MANAGERS_REPO` | _(unset)_ | `owner/repo` for manager doc uploads |
| `HIPPO_GITHUB_BRANCH` | `main` | branch to commit uploaded files to |
| `HIPPO_MAX_UPLOAD_BYTES` | `10485760` | reject multipart uploads larger than this (413) |
| `HIPPO_MAX_DOC_CHARS` | `1000000` | skip docs exceeding this char count before embedding (status: `skipped`) |
| `HIPPO_UI_DIST` | _(unset)_ | path to built UI (`ui/dist`) for FastAPI to serve on one origin; set automatically in the Docker image |
| `HIPPO_SLACK_ENABLED` | `false` | enable the `hippo slack` bot |
| `HIPPO_SLACK_BOT_TOKEN` | _(unset)_ | Slack bot token (`xoxb-…`) |
| `HIPPO_SLACK_APP_TOKEN` | _(unset)_ | Slack app-level token (`xapp-…`, Socket Mode) |

## Authentication

Hippo supports three auth modes, set via `HIPPO_AUTH_MODE`:

- **`none`** (default) — no authentication; every request is treated as a local admin. Suitable for personal use or private networks.
- **`oidc`** — in-app Google sign-in. Users are redirected to `/auth/login`, authenticate with Google, and receive a session cookie. Requires `HIPPO_OIDC_CLIENT_ID`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_SECRET_KEY`, and `HIPPO_PUBLIC_URL`. Optionally restrict to a single Google Workspace domain with `HIPPO_ALLOWED_DOMAIN`.
- **`iap`** — deployed behind [GCP Identity-Aware Proxy](https://cloud.google.com/iap). Hippo verifies the `x-goog-iap-jwt-assertion` header on every request. Requires `HIPPO_IAP_AUDIENCE`.

**Bearer tokens** are accepted in every mode for headless clients (Slack bot, MCP server, CI scripts). Create a token with `hippo token create <email>`.

**Roles:** users have one of three roles — `user` (default), `admin`, or `owner`. Set roles with `hippo role set <email> <role>`. Content is tiered by the folder it lives in — a `user`-tier folder is visible to everyone; an `admin`-tier folder is visible to `admin` and `owner`; an `owner`-tier folder is visible only to `owner`. Admins can manage folders and tokens via the API or the Settings UI. Emails listed in `HIPPO_ADMIN_EMAILS` are always promoted to `owner` on sign-in.

## Settings UI

Every signed-in user can access the Settings view via the gear (⚙) button in the header. From there:

- **Tokens** (everyone) — create, list, and revoke your own personal access tokens (`hk_…`). The plaintext secret is shown exactly once after creation. Use these tokens for MCP clients, the Slack bot, and CI scripts. Each token carries your own role (no escalation).
- **Folders** (admin only) — browse the folder tree, create child folders, rename/delete folders, or trigger a re-sync on filesystem-synced folders. Each folder has a tier (`user`, `admin`, or `owner`) inherited from its parent. Documents live in exactly one folder; upload access is gated by the folder's tier.
- **Users & Roles** (admin only) — list all users and change their role. An admin cannot demote their own account (anti-lockout guard).
- **Status** (admin only) — read-only view of the instance configuration: auth mode, models, repo wiring, MCP/Slack status, and doc/folder/user counts. No secrets are exposed.

**Uploading documents:** click "Add doc" in the header, pick a file, and select one or more destination folders from the modal. Only folders writable by your role are shown (manual folders at or below your tier). The same file can be ingested into multiple folders.

New API endpoints backing the Settings UI: `GET /users`, `PUT /users/{email}/role`, `GET /tokens`, `POST /tokens`, `DELETE /tokens/{id}`, `GET /folders`, `POST /folders`, `PATCH /folders/{id}`, `DELETE /folders/{id}`, `POST /folders/{id}/resync`, `GET /settings/status`.

**Upload to repo:** when `HIPPO_GITHUB_TOKEN` and a repo are configured, files uploaded via `/ingest` are committed to the configured GitHub repo via the Contents API. Without GitHub config, files are ingested directly (unversioned).

**Legacy database note:** SP1 (roles & folder model) introduced a new database schema with no migration. A pre-SP1 database (with `documents.source_id` and no `folders` table) is rejected on startup with a clear "recreate the database" error. Delete the old `.db` file and re-sync your content.

## CLI

    hippo sync [FOLDER] [--watch]   # register+sync folder / re-sync all synced folders
    hippo add FILE                  # ingest one file
    hippo search QUERY              # debug hybrid search
    hippo reindex                   # re-embed after model change
    hippo eval eval/golden.yaml     # retrieval recall@k
    hippo serve                     # FastAPI server
    hippo role set EMAIL ROLE       # set user role (user|admin|owner)
    hippo role list                 # list all users and their roles
    hippo token create EMAIL        # create a bearer token for headless access
    hippo token list EMAIL          # list a user's tokens (never the secret)
    hippo token revoke EMAIL ID     # revoke a token by id
    hippo mcp                       # MCP server over stdio (local single-user, owner)
    hippo slack                     # Slack bot over Socket Mode (read-only Q&A)

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
