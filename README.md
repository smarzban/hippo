# Knowledge Hub

Agentic team brain: feed it markdown/text/Google-Docs-exports, ask it questions in chat.
Spec: `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` · Decisions: `...-decisions.md`

## Quickstart

    uv sync
    export OPENAI_API_KEY=sk-...          # chat + embeddings (defaults)
    uv run hub sync ~/path/to/docs        # ingest a folder
    uv run hub serve                      # API on :8000
    cd ui && npm install && npm run dev   # chat UI on :5173

## Configuration (env, prefix HUB_)

| Var | Default | Notes |
|---|---|---|
| `HUB_DB_PATH` | `hub.db` | the whole brain is this file |
| `HUB_CHAT_MODEL` | `openai:gpt-5.2` | any pydantic-ai model string, e.g. `anthropic:claude-opus-4-8` |
| `HUB_EMBEDDING_MODEL` | `text-embedding-3-small` | `fake` = offline deterministic (dev/tests) |
| `HUB_EMBEDDING_DIM` | `1536` | must match the model; run `hub reindex` after changing |
| `HUB_ENRICH_ENABLED` | `true` | contextual lines + summaries at ingestion (cheap model) |
| `HUB_ENRICH_MODEL` | `openai:gpt-5-mini` | |

## CLI

    hub sync [FOLDER] [--watch]   # register+sync folder / re-sync all sources
    hub add FILE                  # ingest one file
    hub search QUERY              # debug hybrid search
    hub reindex                   # re-embed after model change
    hub eval eval/golden.yaml     # retrieval recall@k
    hub serve                     # FastAPI server

## Tests

    uv run pytest                 # no network: fake embedder + TestModel/FunctionModel
