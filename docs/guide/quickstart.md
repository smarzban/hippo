# Quick Start

Get a working Hippo running locally in about five minutes. This path uses no
authentication and a real model provider so you can see grounded answers
immediately. For a team deployment, read the [Install guide](install/README.md)
instead.

## Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) (the package manager Hippo uses).
- **Node 18+** and `npm` (only to run the chat UI in dev).
- A **model provider**: either an OpenAI API key, or a local [Ollama](https://ollama.com)
  install. You can also run fully offline with fake embeddings (see the last section).

## 1. Install dependencies

```bash
git clone <your-hippo-remote> hippo && cd hippo
uv sync
cp .env.example .env
```

## 2. Configure a model provider

Edit `.env`. Pick one:

**Option A — OpenAI (default):**

```bash
OPENAI_API_KEY=sk-...
```

**Option B — local Ollama** (Ollama Cloud does not serve embeddings, so use a
local embedding model):

```bash
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
HIPPO_CHAT_MODEL=openai:gpt-oss:120b-cloud
HIPPO_ENRICH_MODEL=openai:gpt-oss:120b-cloud
HIPPO_EMBEDDING_MODEL=nomic-embed-text
HIPPO_EMBEDDING_DIM=768
```

Then `ollama pull nomic-embed-text` (and `ollama signin` if you use cloud models).

> **Why the `OPENAI_*` vars are special:** they're read from the process
> environment, not auto-loaded from `.env`. Load `.env` into your shell before
> starting Hippo (next step). The `HIPPO_*` vars *are* read from `.env`.

## 3. Index some documents and start the server

```bash
set -a; source .env; set +a            # load OPENAI_* into the environment
uv run hippo sync eval/fixtures        # ingest the bundled sample docs (or point at your own folder)
uv run hippo serve                     # API on http://localhost:8000
```

In a second terminal, start the chat UI:

```bash
cd ui && npm install && npm run dev    # UI on http://localhost:5173 (proxies to :8000)
```

Open <http://localhost:5173> and ask a question. Because `HIPPO_AUTH_MODE` is
`none` by default, you're treated as a local owner and can see everything — fine
for a personal trial on your own machine.

## 4. Ask a question

Try one of the suggested prompts, or ask about whatever you indexed. You'll get
an answer with **footnote-style citations**; click a citation to open the source
document at the cited section. If an answer has no citations, Hippo flags it so
you know to verify it independently.

## Fully offline (no model provider)

For development or a quick look with **no network and no API key**, use the
deterministic fake embedder:

```bash
HIPPO_EMBEDDING_MODEL=fake uv run hippo sync eval/fixtures
HIPPO_EMBEDDING_MODEL=fake uv run hippo serve
```

Retrieval works (search + citations); only the *chat generation* needs a real
model. This is the same mode the test suite uses.

## Next steps

- **Deploy for a team:** [Install guide](install/README.md) → [Docker](install/docker.md)
  or [Production hardening](install/production.md).
- **Turn on authentication:** [Auth setup](install/auth-setup.md).
- **Learn the features:** [User guide](users/README.md).
- **Understand the internals:** [Technical docs](technical/README.md).
