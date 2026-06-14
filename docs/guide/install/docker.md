# Install with Docker Compose

The recommended way to run Hippo for a team. A multi-stage image builds the
React UI and installs the Python app, then a single container serves **both the
API and the UI on one origin** at `:8000`. State lives in a named volume so it
survives restarts and rebuilds.

## 1. Create your `.env`

The compose file reads `.env` (via `env_file`). At minimum set a model provider:

```bash
cp .env.example .env
# edit .env: set OPENAI_API_KEY=sk-...   (or the Ollama block — see below)
```

See [Configuration](configuration.md) for every option. If you don't want to use
a `.env` file at all, remove the `env_file:` line from `compose.yaml` and wire
environment variables your own way.

## 2. Build and run

```bash
docker compose up --build
```

Open <http://localhost:8000>. The image sets `HIPPO_UI_DIST=/app/ui/dist`, so
the chat UI is served from the same origin as the API — no separate UI server.

## What the compose setup does

From `compose.yaml`:

- **Port `8000:8000`** — the API + UI.
- **Named volume `hippo-data` → `/app/data`**, with `HIPPO_DB_PATH=/app/data/hippo.db`.
  Your knowledge base persists here across `docker compose down`/`up`. Deleting
  the volume deletes the brain.
- **`extra_hosts: host.docker.internal:host-gateway`** — lets the container reach
  a model provider running on the host (e.g. Ollama).

The image runs as a non-root user (`appuser`, uid 10001); `/app/data` is
pre-created and owned by it so the SQLite file stays writable on a mounted volume.

## Using a host Ollama from the container

Point the provider at the host gateway in `.env`:

```bash
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
HIPPO_CHAT_MODEL=openai:gpt-oss:120b-cloud
HIPPO_EMBEDDING_MODEL=nomic-embed-text
HIPPO_EMBEDDING_DIM=768
```

## Indexing documents

The container starts `hippo serve`. To ingest content you can either:

- **Upload through the UI** ("Add doc" → pick file → choose folders), or
- **Mount a directory and sync it.** Bind-mount your docs into the container,
  set `HIPPO_SOURCE_ROOTS` to the in-container path, and create a synced folder
  via the Settings → Folders tab (or `POST /folders` with `origin: folder`). See
  [Documents & folders](../users/documents-and-folders.md).

> A run-time CLI command inside the container looks like:
> `docker compose exec hippo uv run --no-sync hippo <command>`.

## Going to production

Behind a reverse proxy with TLS, and with real authentication, follow the
[Production hardening checklist](production.md). In particular set
`HIPPO_SECRET_KEY`, choose a non-`none` `HIPPO_AUTH_MODE`, and set
`HIPPO_PUBLIC_URL` to the externally reachable `https://…` base.

## Upgrading the image

```bash
docker compose pull   # or: git pull
docker compose up --build -d
```

The named volume (your data) is untouched by a rebuild. If you change the
embedding model or dimension, you must re-embed — see [Upgrading](upgrading.md).
