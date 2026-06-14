# Install locally with `uv`

The path for single users and for developing Hippo. You run the API with `uv`
and (in dev) the React UI with the Vite dev server.

## 1. Install and configure

```bash
git clone <your-hippo-remote> hippo && cd hippo
uv sync                       # creates .venv and installs the app + deps
cp .env.example .env          # then edit it
```

Set a [model provider](configuration.md#model-provider) in `.env`
(`OPENAI_API_KEY=sk-...`, the Ollama block, or `HIPPO_EMBEDDING_MODEL=fake` for
offline).

## 2. Load the environment and run

The `OPENAI_*` variables are read from the process environment, not `.env`, so
load `.env` into your shell first:

```bash
set -a; source .env; set +a
uv run hippo sync eval/fixtures   # ingest sample docs, or point at your own folder
uv run hippo serve                # API on http://localhost:8000
```

For the UI in development:

```bash
cd ui && npm install && npm run dev   # http://localhost:5173, proxies API to :8000
```

The Vite dev server proxies `/chat`, `/ingest`, `/documents`, `/folders`,
`/users`, `/tokens`, `/settings`, `/config`, `/setup`, `/me`, and `/auth` to the
API on `:8000`, so the two run side by side in dev.

## Serving the built UI on one origin (no Vite)

To mimic the single-origin production layout locally, build the UI and point
Hippo at it:

```bash
cd ui && npm run build && cd ..
HIPPO_UI_DIST=ui/dist uv run hippo serve   # API + UI both on :8000
```

## Common local commands

```bash
uv run hippo sync <folder> [--watch]   # register + sync a folder (‑‑watch re-syncs on change)
uv run hippo add <file>                # ingest a single file
uv run hippo search "<query>"          # debug hybrid search from the CLI
uv run hippo reindex                   # re-embed everything (after a model/dim change)
uv run hippo backup snapshot.db        # consistent single-file snapshot
uv run pytest                          # the test suite (fast, zero network)
```

The full command list is in the [CLI reference](../technical/cli.md).

## Running the test suite

```bash
uv run pytest          # Python: must stay zero-network and fast
cd ui && npm test      # the Vitest UI suites
```

> **Heads up:** if your `.env` sets `HIPPO_AUTH_MODE=password` with a live
> `HIPPO_SECRET_KEY`/`HIPPO_SETUP_TOKEN`, sourcing it can leak into the test
> process and break tests. Run pytest with a clean environment if so:
> `env -i HOME="$HOME" PATH="$PATH" uv run pytest`.

## Where things live

- **Database:** `HIPPO_DB_PATH` (default `hippo.db` in the working directory).
- **Source:** `src/hippo/` (see the [Architecture](../technical/architecture.md)).
- **UI:** `ui/` (Vite + React).

Next: turn on [authentication](auth-setup.md), or read the
[Configuration reference](configuration.md).
