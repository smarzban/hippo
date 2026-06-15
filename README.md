# Hippo

**An agentic organizational knowledge base. Feed it your team's docs; ask questions in chat; get answers grounded *only* in those docs, with citations.**

[![CI](https://github.com/smarzban/hippo/actions/workflows/ci.yml/badge.svg)](https://github.com/smarzban/hippo/actions/workflows/ci.yml)

Hippo is a **self-hosted, agentic retrieval-augmented generation (RAG) knowledge base**. It indexes your Markdown / text / HTML / `.docx` (incl. Google-Docs exports) and answers questions about them in plain language — citing the exact `[path > section]` each claim comes from, and refusing to answer from anything it wasn't given. It's role-governed, scales from one person to a whole org, and is reachable from a web chat UI, an MCP server, and Slack.

## Why Hippo

- **Grounded answers only.** Every claim is cited to a source document; the agent is constrained never to improvise, and document text is fed to the model inside a prompt-injection boundary. If it can't find support, it says so.
- **Access control that holds.** Three-tier RBAC (`user` / `admin` / `owner`) plus per-folder tiers, enforced in the data layer — so chat, MCP, and Slack all return only what you're allowed to see.
- **Hybrid retrieval.** Keyword (FTS5 BM25) + semantic (vector KNN) search, fused — good at both exact identifiers and paraphrase.
- **Many front doors.** A React chat UI, an MCP server for Claude Code / Desktop, and a Slack bot — all over the same grounded, role-filtered engine.
- **Self-hosted & simple.** A self-hosted alternative to hosted "chat-with-your-docs" / RAG services — your data stays in one SQLite file (`sqlite-vec` + FTS5) you control. No external vector DB. Runs fully offline with fake embeddings.
- **Team-ready.** Four auth modes (`none` / `oidc` / `iap` / `password`), a browser first-run setup wizard, and folder-scoped uploads.

## Quick start

Requires **Python 3.12+**, [`uv`](https://docs.astral.sh/uv/), **Node 18+**, and an OpenAI API key (or local [Ollama](https://ollama.com) — see the docs).

```bash
git clone <your-hippo-remote> hippo && cd hippo
uv sync
cp .env.example .env             # set OPENAI_API_KEY=sk-...
set -a; source .env; set +a      # OPENAI_* are read from the environment, not .env
uv run hippo sync eval/fixtures  # index the bundled sample docs (or your own folder)
uv run hippo serve               # API on http://localhost:8000
```

In a second terminal, start the chat UI:

```bash
cd ui && npm install && npm run dev   # http://localhost:5173 (proxies to :8000)
```

Open <http://localhost:5173> and ask a question — you'll get an answer with clickable citations.

> **No API key handy?** Run fully offline with `HIPPO_EMBEDDING_MODEL=fake` — retrieval and citations work; only chat *generation* needs a real model.

Full setup (Docker, Ollama, authentication, production) → **[Install guide](docs/guide/install/README.md)**.

## Documentation

The complete guide lives in **[`docs/guide/`](docs/guide/README.md)**:

- **New here?** → [Quick start](docs/guide/quickstart.md)
- **Installing / operating it** → [Install guide](docs/guide/install/README.md) (Docker, local, configuration, auth, production)
- **Using it** (asking questions, adding docs, folders, admin/owner tasks) → [User guide](docs/guide/users/README.md)
- **How it works** (architecture, RAG pipeline, storage, API, RBAC, the agent) → [Technical docs](docs/guide/technical/README.md)

## Configuration

Configured via environment variables (prefix `HIPPO_`); `OPENAI_*` are read from the process environment. The most common:

| Var | Default | Notes |
|---|---|---|
| `HIPPO_CHAT_MODEL` | `openai:gpt-5.2` | Any pydantic-ai model string (e.g. `anthropic:claude-opus-4-8`). |
| `HIPPO_EMBEDDING_MODEL` | `text-embedding-3-small` | `fake` = offline. Env-only; `hippo reindex` to change. |
| `HIPPO_AUTH_MODE` | `none` | `none` \| `oidc` \| `iap` \| `password`. |
| `HIPPO_DB_PATH` | `hippo.db` | The whole knowledge base is this one file. |

The full reference (limits, auth wiring, integrations, the env-vs-DB-overlay rules) is in **[Configuration](docs/guide/install/configuration.md)**.

## Authentication

Four modes, set via `HIPPO_AUTH_MODE`: **`none`** (open; dev/private only), **`password`** (email + password, argon2id, lockout), **`oidc`** (Google sign-in), and **`iap`** (behind GCP Identity-Aware Proxy). Bearer tokens work in every mode for headless clients. The first owner is created through the browser **first-run setup wizard**. Details and setup → **[Authentication](docs/guide/install/auth-setup.md)**.

## Integrations

- **MCP** — query Hippo from Claude Code / Desktop, role-filtered by your token. Mounted at `/mcp`, or run `hippo mcp` over stdio. → [Using MCP](docs/guide/users/using-mcp.md)
- **Slack** — ask Hippo in a DM or with `@Hippo` in a channel (channel mentions are restricted to `user`-tier content). Runs as a separate Socket-Mode process. → [Using Slack](docs/guide/users/using-slack.md)

## CLI

```bash
uv run hippo sync [FOLDER] [--watch]   # ingest / re-sync folders
uv run hippo serve                     # the API (+ UI when built)
uv run hippo reindex                   # re-embed after a model change
uv run hippo backup snapshot.db        # consistent single-file snapshot
uv run hippo mcp | slack               # the MCP (stdio) / Slack surfaces
```

Every command (roles, tokens, password bootstrap, eval) → **[CLI reference](docs/guide/technical/cli.md)**.

## Development

```bash
uv run pytest          # Python suite — fast, zero network
cd ui && npm test      # the Vitest UI suites
```

CI (`pytest` + `npm run build`) runs on every PR. Test discipline, the eval harness, and the architecture are covered in **[Development](docs/guide/technical/development.md)** and the [Technical docs](docs/guide/technical/README.md).

## License

This project does **not** yet declare an open-source license. Until one is added, default copyright applies (all rights reserved). _Maintainers: add a `LICENSE` file and state it here._
