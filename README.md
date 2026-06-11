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

## CLI

    hippo sync [FOLDER] [--watch]   # register+sync folder / re-sync all sources
    hippo add FILE                  # ingest one file
    hippo search QUERY              # debug hybrid search
    hippo reindex                   # re-embed after model change
    hippo eval eval/golden.yaml     # retrieval recall@k
    hippo serve                     # FastAPI server

## Tests

    uv run pytest                 # no network: fake embedder + TestModel/FunctionModel
