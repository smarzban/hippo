# Configuration reference

Hippo is configured through environment variables. There are two families:

- **`HIPPO_*`** — read by Hippo via pydantic-settings (the `HIPPO_` prefix). These
  *are* loaded from a `.env` file in the working directory.
- **`OPENAI_*`** — read from the **process environment** by the model SDK, *not*
  auto-loaded from `.env`. Load `.env` into your shell first: `set -a; source .env; set +a`.

`.env.example` lists every setting with its default commented out; uncomment to
override.

## Model provider

Hippo talks to any OpenAI-compatible chat+embeddings API.

**OpenAI (default):**

```bash
OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=        # leave unset for OpenAI
```

**Local/Cloud Ollama** via its OpenAI-compatible endpoint (Ollama Cloud does not
serve embeddings, so use a *local* embedding model):

```bash
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
HIPPO_CHAT_MODEL=openai:gpt-oss:120b-cloud
HIPPO_ENRICH_MODEL=openai:gpt-oss:120b-cloud
HIPPO_EMBEDDING_MODEL=nomic-embed-text
HIPPO_EMBEDDING_DIM=768
```

> The `20b-cloud` Ollama model returns empty content through the `/v1` API; use
> `120b-cloud` for chat/enrichment.

**Offline / tests:** `HIPPO_EMBEDDING_MODEL=fake` uses a deterministic local
embedder — no network, no key. Retrieval works; only chat generation needs a
real model.

## Core settings

| Var | Default | Notes |
|---|---|---|
| `HIPPO_DB_PATH` | `hippo.db` | The entire knowledge base is this one SQLite file. |
| `HIPPO_CHAT_MODEL` | `openai:gpt-5.2` | Any pydantic-ai model string, e.g. `anthropic:claude-opus-4-8`. Live-overridable (see below). |
| `HIPPO_EMBEDDING_MODEL` | `text-embedding-3-small` | `fake` = offline. **Env-only** (see the warning below). |
| `HIPPO_EMBEDDING_DIM` | `1536` | Must match the model. **Env-only**; `hippo reindex` after changing. |
| `HIPPO_EMBED_TIMEOUT_S` | `60` | Per-request bound on the embedding endpoint. |
| `HIPPO_EMBED_MAX_RETRIES` | `2` | Embedding client retry budget on transient failures. |
| `HIPPO_ENRICH_ENABLED` | `true` | Generate a per-doc summary + per-chunk context line at ingest. |
| `HIPPO_ENRICH_MODEL` | `openai:gpt-5-mini` | The (cheap) model used for enrichment. |
| `HIPPO_CHUNK_MAX_CHARS` | `3000` | ~750 tokens per chunk. |
| `HIPPO_CHUNK_OVERLAP_CHARS` | `200` | Overlap tail prepended between chunks. |
| `HIPPO_MAX_TOOL_CALLS` | `15` | The agent's tool-call budget per question. |
| `HIPPO_SEARCH_TOP_K` | `8` | Chunks retrieved per search. |

## Ingestion limits

| Var | Default | Notes |
|---|---|---|
| `HIPPO_MAX_UPLOAD_BYTES` | `10485760` (10 MB) | Reject larger multipart uploads (413). |
| `HIPPO_MAX_DOC_CHARS` | `1000000` | Skip docs over this size before embedding (status `skipped`). |
| `HIPPO_MAX_DECOMPRESSED_BYTES` | `100000000` | `.docx` ZIP-bomb guard (uncompressed size). |

## Authentication

| Var | Default | Notes |
|---|---|---|
| `HIPPO_AUTH_MODE` | `none` | `none` \| `oidc` \| `iap` \| `password`. See [Auth setup](auth-setup.md). |
| `HIPPO_ALLOWED_DOMAIN` | _(unset)_ | Restrict sign-in to one Google Workspace domain. |
| `HIPPO_ADMIN_EMAILS` | _(unset)_ | Comma-separated emails promoted to **owner** on sign-in (bootstrap). |
| `HIPPO_SECRET_KEY` | _(required for oidc/password)_ | Random secret for session-cookie signing. |
| `HIPPO_OIDC_CLIENT_ID` | _(required for oidc)_ | Google OAuth2 client ID. Live-overridable. |
| `HIPPO_OIDC_CLIENT_SECRET` | _(required for oidc)_ | Google OAuth2 client secret. **Secret — env-only.** |
| `HIPPO_PUBLIC_URL` | `http://localhost:8000` | Externally reachable base; forms the OAuth redirect URI and sets the cookie `Secure` flag. Use your `https://…` base in oidc. |
| `HIPPO_IAP_AUDIENCE` | _(required for iap)_ | GCP IAP backend-service audience. Live-overridable. |
| `HIPPO_SETUP_TOKEN` | _(unset)_ | First-run wizard gate. If unset, a random token is printed to stderr at startup. **Secret — env-only.** |

## Sources, serving, integrations

| Var | Default | Notes |
|---|---|---|
| `HIPPO_SOURCE_ROOTS` | _(unset)_ | Colon-separated allowed mount directories. **Required for any filesystem folder mount, in every mode** — a folder can only sync from inside an allowlisted root. |
| `HIPPO_UI_DIST` | _(unset)_ | Path to the built UI (`ui/dist`) to serve on one origin. Set automatically in the Docker image. |
| `HIPPO_MCP_ENABLED` | `true` | Mount the `/mcp` MCP server on the API. |
| `HIPPO_SLACK_ENABLED` | `false` | `hippo slack` refuses to start unless `true`. |
| `HIPPO_SLACK_BOT_TOKEN` | _(unset)_ | Slack bot token (`xoxb-…`). |
| `HIPPO_SLACK_APP_TOKEN` | _(unset)_ | Slack app-level token (`xapp-…`, Socket Mode). |

## Env vs. the live config overlay

Owners can change *some* operational settings at runtime without editing env —
they're stored in a `config` table in the database and the DB value wins over the
env default. These **DB-overridable** keys are: `auth_mode`, `chat_model`,
`enrich_model`, `allowed_domain`, `oidc_client_id`, `public_url`, `iap_audience`.

- **`chat_model`** is read **live per request** — change it and the next chat
  uses the new model, no restart.
- The others take effect on the next `hippo serve` restart.
- Change them via **Settings → System config** (owner) or `PUT /config`.

See [Config & setup internals](../technical/config-and-setup.md) for the resolver.

### Two things that are NEVER in the database

1. **Secrets** — `OPENAI_API_KEY`, `HIPPO_OIDC_CLIENT_SECRET`, `HIPPO_SECRET_KEY`,
   `HIPPO_SETUP_TOKEN`, Slack tokens. They are never stored in the DB and never
   returned by any API endpoint.

2. **`embedding_model` / `embedding_dim`** — these are **env-only**. The vector
   space and the `chunk_vec` table width are fixed when the index is created and
   only change via `hippo reindex` (which reads the environment). A DB override
   could neither take effect nor stay accurate after a reindex, so the env-built
   embedder is the single source of truth. To change embeddings: set
   `HIPPO_EMBEDDING_MODEL`/`HIPPO_EMBEDDING_DIM` and run `hippo reindex`.

> If you set a non-default embedding model, you **must** also set the matching
> `HIPPO_EMBEDDING_DIM`. Hippo stamps the model + dimension into the database on
> first ingest and refuses to mix embedding spaces — see [Upgrading](upgrading.md)
> and [Embeddings](../technical/embeddings.md).
