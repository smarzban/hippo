# Hippo

Agentic team brain: feed it markdown/text/Google-Docs-exports, ask it questions in chat.
Spec: `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` · Decisions: `...-decisions.md`

## Quickstart

    uv sync
    export OPENAI_API_KEY=sk-...          # chat + embeddings (defaults)
    uv run hippo sync ~/path/to/docs        # ingest a folder
    uv run hippo serve                      # API on :8000
    cd ui && npm install && npm run dev   # chat UI on :5173

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

## Authentication

Hippo supports three auth modes, set via `HIPPO_AUTH_MODE`:

- **`none`** (default) — no authentication; every request is treated as a local admin. Suitable for personal use or private networks.
- **`oidc`** — in-app Google sign-in. Users are redirected to `/auth/login`, authenticate with Google, and receive a session cookie. Requires `HIPPO_OIDC_CLIENT_ID`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_SECRET_KEY`, and `HIPPO_PUBLIC_URL`. Optionally restrict to a single Google Workspace domain with `HIPPO_ALLOWED_DOMAIN`.
- **`iap`** — deployed behind [GCP Identity-Aware Proxy](https://cloud.google.com/iap). Hippo verifies the `x-goog-iap-jwt-assertion` header on every request. Requires `HIPPO_IAP_AUDIENCE`.

**Bearer tokens** are accepted in every mode for headless clients (Slack bot, MCP server, CI scripts). Create a token with `hippo token create <email>`.

**Roles:** users have one of three roles — `developer` (default), `manager`, or `admin`. Set roles with `hippo role set <email> <role>`. Sources can be restricted to `managers` and above via the `access` field on `/sources`. Admins can manage sources and tokens via the API.

**Upload to repo:** when `HIPPO_GITHUB_TOKEN` and a repo are configured, files uploaded via `/ingest` are committed to the configured GitHub repo via the Contents API (`uploads/` prefix). Without GitHub config, files are ingested directly (unversioned).

## CLI

    hippo sync [FOLDER] [--watch]   # register+sync folder / re-sync all sources
    hippo add FILE                  # ingest one file
    hippo search QUERY              # debug hybrid search
    hippo reindex                   # re-embed after model change
    hippo eval eval/golden.yaml     # retrieval recall@k
    hippo serve                     # FastAPI server
    hippo role set EMAIL ROLE       # set user role (developer|manager|admin)
    hippo role list                 # list all users and their roles
    hippo token create EMAIL        # create a bearer token for headless access
    hippo token list EMAIL          # list a user's tokens (never the secret)
    hippo token revoke EMAIL ID     # revoke a token by id

## Tests

    uv run pytest                 # no network: fake embedder + TestModel/FunctionModel
