# Hippo — agentic team knowledge base

Feed it markdown/text/Google-Docs-exports; ask it questions in chat; it answers **only**
from the indexed docs, with `[path > section]` citations. Personal-first, team-ready.

## Read these before changing anything significant

- `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` — the spec (what & how)
- `docs/superpowers/specs/2026-06-11-knowledge-hub-decisions.md` — decision log (why; 9 ADRs incl. all rejected alternatives)
- `docs/superpowers/plans/2026-06-11-knowledge-hub.md` — the 15-task build plan (code has a few post-plan hardening fixes the plan text doesn't show)

Naming: the project was "knowledgeHub" during design (docs keep that name); the product/package is **hippo** (from hippocampus).

## Architecture (src/hippo/)

```
config.py      Settings, env prefix HIPPO_ (pydantic-settings)
db.py          connect() -> sqlite3: schema, WAL, sqlite-vec, FTS5 + sync triggers
chunking.py    chunk_markdown(): heading-aware, atomic code fences, char-based ~750-token chunks
embeddings.py  Embedder protocol; OpenAIEmbedder (default text-embedding-3-small); FakeEmbedder (deterministic, tests/offline)
storage.py     Storage(con, embedder): ALL SQL lives here. upsert/delete/get/list docs,
               search_hybrid (FTS5 BM25 + vec KNN merged via RRF, k=60), grep (raises ValueError on bad regex)
parsers.py     .md/.txt/.html -> (title, canonical markdown). SUPPORTED set is the gate.
ingest.py      Ingestor: parse->hash dedupe->chunk->enrich->embed+index (1 txn/doc, per-file isolation);
               sync_folder() handles deletions + IGNORED_EXTENSIONS/DIRS noise filter
enrich.py      Enricher: doc summary + contextual line per chunk (cheap model; embedding input = context+"\n"+chunk; stored chunk text stays raw)
agent.py       build_agent(model) -> Pydantic AI agent, deps=HubDeps(store). 4 tools: search/read_document/list_documents/grep.
               System prompt enforces cite-everything + never-improvise. defer_model_check=True (don't remove: construction must not need API keys)
api.py         build_app(settings, model_override=None): /chat streams Vercel AI protocol via VercelAIAdapter.dispatch_request
               (deps + usage_limits kwargs work on pydantic-ai 1.107). /ingest /documents /sources /health. verify_request = auth stub on every route.
cli.py         Typer: sync [--watch] / add / search / reindex / serve / eval
```

`ui/` — Vite + React 19 + `@ai-sdk/react` v2 `useChat` + `DefaultChatTransport({api:"/chat"})`.
Vite dev-server proxies /chat,/ingest,/documents,/sources to :8000. Tool parts render as progress lines.

## Commands

```bash
uv run pytest                      # full suite (~46 tests, <2s, ZERO network — must stay that way)
uv run hippo sync <folder>         # ingest; re-run with no arg re-syncs all registered sources
uv run hippo serve                 # API :8000
cd ui && npm run dev               # chat UI :5173
uv run hippo eval eval/golden.yaml # retrieval recall@k regression gate
uv run hippo reindex               # re-embed after changing HIPPO_EMBEDDING_MODEL/DIM
```

Config via env (`HIPPO_` prefix) or `.env`: see README table. `HIPPO_EMBEDDING_MODEL=fake` = offline mode.

## Hard rules / gotchas

- **Tests never hit the network.** Use `FakeEmbedder` + pydantic-ai `TestModel`/`FunctionModel`; agent/api/enrich
  tests set `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False`. Keep it.
- **No SQL outside storage.py** (cli.py `reindex` is the one tolerated exception). The agent/API/ingest call the
  Storage interface — this is the Postgres exit ramp; don't erode it.
- Test dbs must NOT live inside folders that get synced (sqlite WAL files pollute rglob) — use a separate tmp dir
  for the db (see tests/test_ingest.py fixture).
- `Agent(...)` constructions need `defer_model_check=True` or import-time crashes without API keys.
- Chat protocol payloads require `"trigger": "submit-message"` (Vercel AI SubmitMessage schema).
- `chunk_vec` dim is fixed at table creation; changing embedding dim requires `hippo reindex` (drops/recreates table).
- TDD discipline: failing test first; commit per green step.

## State (2026-06-11)

v1 complete and merged to main: storage/hybrid search, ingestion (folder sync + upload), enrichment,
agent, API, CLI, React UI, eval harness. 46/46 tests, eval 4/4 on seed fixtures, UI builds clean.

**Deferred (spec §12):** Google Drive connector (interface: `list_items()` + `fetch()` -> markdown), Slack bot
(consumes POST /chat), PDF/docx parsers, Postgres+pgvector migration (reimplement Storage), real auth
(implement `verify_request` in api.py), hierarchical summaries, GraphRAG.

**Next obvious steps:** replace `eval/golden.yaml` seed with ~20 real-team-doc questions; first real-corpus
sync + eval run; Drive connector.
