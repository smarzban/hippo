# Installing Hippo

Hippo is a single Python application that serves an HTTP API (and, optionally,
the built React UI on the same origin). Its entire state is one SQLite file, so
there is no database server to provision. This section covers every supported
way to install it and all the configuration that goes with it.

## Choose your path

| Method | Best for | Guide |
|---|---|---|
| **Docker Compose** | Teams; the simplest reproducible deployment. One container serves API + UI on `:8000`. | [docker.md](docker.md) |
| **Local with `uv`** | Single users, development, and hacking on the code. | [local-uv.md](local-uv.md) |
| **Production** | A network-reachable, multi-user instance with TLS and auth. Builds on Docker or local. | [production.md](production.md) |

If you just want to try it on your own machine, the [Quick Start](../quickstart.md)
is faster than any of these.

## Prerequisites by method

- **Docker:** Docker Engine with Compose v2 (`docker compose`). Nothing else —
  the image builds the UI and installs Python deps itself.
- **Local:** Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and (for the dev
  UI) Node 18+ with `npm`.
- **A model provider** in all cases: an OpenAI API key, a local/cloud Ollama
  endpoint via its OpenAI-compatible API, or `fake` embeddings for an offline
  trial. See [Configuration → Model provider](configuration.md#model-provider).

## The other reference pages

- **[configuration.md](configuration.md)** — the complete `HIPPO_*` environment
  variable reference, plus how the `OPENAI_*` provider variables work and the
  config precedence rules (env vs. the live DB config overlay).
- **[auth-setup.md](auth-setup.md)** — the four authentication modes (`none`,
  `oidc`, `iap`, `password`), when to use each, the exact env they require, and
  the first-run setup wizard.
- **[production.md](production.md)** — a hardening checklist: TLS, reverse
  proxies, the session secret, the source-roots allowlist, backups, and logging.
- **[upgrading.md](upgrading.md)** — upgrading, the legacy-database guard, and
  when you must `hippo reindex`.

## The shape of a running instance

- **API + UI:** `hippo serve` runs the FastAPI app on `:8000`. In Docker (or any
  build where `HIPPO_UI_DIST` points at `ui/dist`) the same process serves the
  React UI on that one origin. In local dev you instead run the Vite dev server
  on `:5173`, which proxies API calls to `:8000`.
- **MCP server:** mounted at `/mcp` on the same app (toggle with
  `HIPPO_MCP_ENABLED`), and also runnable over stdio with `hippo mcp`. See
  [Using MCP](../users/using-mcp.md).
- **Slack bot:** a separate process, `hippo slack`, that connects out over Socket
  Mode — no public endpoint needed. See [Using Slack](../users/using-slack.md).

Everything persists in the one SQLite file at `HIPPO_DB_PATH` (default
`hippo.db`). Back it up with [`hippo backup`](../technical/cli.md).
