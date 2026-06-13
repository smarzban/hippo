# Hippo — agentic team knowledge base

Feed it markdown/text/Google-Docs-exports; ask it questions in chat; it answers **only**
from the indexed docs, with `[path > section]` citations. Personal-first, team-ready.

## Read these before changing anything significant

- `docs/superpowers/specs/2026-06-11-knowledge-hub-design.md` — the spec (what & how)
- `docs/superpowers/specs/2026-06-11-knowledge-hub-decisions.md` — decision log (why; 9 ADRs incl. all rejected alternatives)
- `docs/superpowers/plans/2026-06-11-knowledge-hub.md` — the 15-task build plan (code has a few post-plan hardening fixes the plan text doesn't show)
- `docs/superpowers/plans/2026-06-12-roadmap.md` — **current roadmap / action list** (post-v1 hardening + features; what's next and in what order)

Naming: the project was "knowledgeHub" during design (docs keep that name); the product/package is **hippo** (from hippocampus).

## Architecture (src/hippo/)

```
agent.py       build_agent(model) -> Pydantic AI agent, deps=HubDeps(store, role). 4 tools: search/read_document/list_documents/grep.
               Tool output is framed as ⟦untrusted document data⟧…⟦end⟧ (prompt-injection boundary).
               System prompt enforces cite-everything + never-improvise + untrusted-content rule. defer_model_check=True (don't remove: construction must not need API keys)
api.py         build_app(settings, model_override=None): /chat streams Vercel AI protocol via VercelAIAdapter.dispatch_request
               (deps + usage_limits kwargs work on pydantic-ai 1.107). verify_request real: modes none|oidc|iap + bearer tokens every mode.
               require_admin guards POST/DELETE /sources. /me endpoint. /auth/login,/auth/callback,/auth/logout (oidc).
               /ingest: size-checked against HIPPO_MAX_UPLOAD_BYTES (413) and HIPPO_MAX_DOC_CHARS (skipped); takes repo field: commits to GitHub when configured (status "committed") else direct unversioned ingest.
               /sources allowlist via HIPPO_SOURCE_ROOTS; DELETE /sources/{id}.
               Serves ui/dist as static files when HIPPO_UI_DIST is set (single origin, :8000).
               Mounts FastMCP server at /mcp (HIPPO_MCP_ENABLED, default true) with _McpBearerAuth middleware (bearer token → role via _mcp_role contextvar).
mcp_server.py  FastMCP server exposing search/read_document/list_documents/grep; mounted at /mcp in api.py with bearer-token auth + role filtering via the _mcp_role contextvar; `hippo mcp` runs it over stdio (as admin, no token).
auth.py        AuthenticatedUser(email, role), AuthError, check_domain, resolve_role (shared identity→role: normalize+domain-gate+ensure_user+admin-bootstrap, used by api.py and slack_bot.py),
               IapVerifier (ES256 IAP assertions, injectable key_fetcher),
               validate_google_id_token (claims-only, code-flow tokens). Mode wiring lives in api.py: none|oidc|iap + bearer tokens any mode.
               Role FILTERING lives in storage.py, not here.
chunking.py    chunk_markdown(): heading-aware, atomic code fences, char-based ~750-token chunks; overlap tail is re-checked against max_chars before prepending
cli.py         Typer: sync [--watch] / add / search / reindex / serve / mcp / slack / eval / backup / role set/list / token create
slack_bot.py   Slack Q&A bot (roadmap item 7): Socket Mode via slack-bolt. Pure helpers
               (surface_role/build_history/format_answer/answer_question) + handle_event
               adapter (tested with a fake client) + build_slack_app wiring. Split-by-surface
               access: DM=asker's role, channel @mention=everyone-only. `hippo slack` runs it.
config.py      Settings, env prefix HIPPO_ (pydantic-settings)
db.py          connect() -> sqlite3: schema, WAL, sqlite-vec, FTS5 + sync triggers
embeddings.py  Embedder protocol; OpenAIEmbedder (default text-embedding-3-small); FakeEmbedder (deterministic, tests/offline)
enrich.py      Enricher: doc summary + contextual line per chunk (cheap model; embedding input = context+"\n"+chunk; stored chunk text stays raw)
github.py      GitHubContentsClient.put_file: upload-to-repo via Contents API (1 call/file)
ingest.py      Ingestor: parse->hash dedupe->chunk->enrich->embed+index (1 txn/doc, per-file isolation);
               sync_folder() handles deletions + IGNORED_EXTENSIONS/DIRS noise filter
parsers.py     .md/.txt/.html/.docx -> (title, canonical markdown). SUPPORTED set is the gate.
               .docx via mammoth (docx -> HTML -> markdown, heading styles preserved).
               parse_bytes(filename, data) is the canonical bytes entry point (used by /ingest).
storage.py     Storage(con, embedder): ALL SQL lives here. upsert/delete/get/list docs,
               search_hybrid (FTS5 BM25 + vec KNN merged via RRF, k=60), grep (raises ValueError on bad regex/timeout/pattern-too-long).
               backup(path) via VACUUM INTO for consistent snapshots.
               Users/roles (ensure_user, set_role, list_users), hashed tokens (create_token, resolve_token),
               source access levels ('everyone'|'managers', access=None preserves on re-register), delete_source.
               Role-filtered retrieval: search_hybrid/grep/list_documents/get_document take keyword-only `role` with NO default.
```

`ui/` — Vite + React 19 + `@ai-sdk/react` v2 `useChat` + `DefaultChatTransport({api:"/chat"})`.
Vite dev-server proxies /chat,/ingest,/documents,/sources to :8000. Tool parts render as progress lines.

## Commands

```bash
uv run pytest                      # full suite (190+ tests, <5s, ZERO network — must stay that way)
uv run hippo sync <folder>         # ingest; re-run with no arg re-syncs all registered sources
uv run hippo serve                 # API :8000
cd ui && npm run dev               # chat UI :5173
uv run hippo eval eval/golden.yaml # retrieval recall@k regression gate
uv run hippo reindex               # re-embed after changing HIPPO_EMBEDDING_MODEL/DIM
uv run hippo backup <path>         # consistent single-file snapshot via VACUUM INTO
docker compose up --build          # build + run API+UI on :8000 (set .env first)
# CI: .github/workflows/ci.yml runs pytest + npm run build on every PR
```

Config via env (`HIPPO_` prefix) or `.env`: see README table. `HIPPO_EMBEDDING_MODEL=fake` = offline mode.

## Hard rules / gotchas

- **Tests never hit the network.** Use `FakeEmbedder` + pydantic-ai `TestModel`/`FunctionModel`; agent/api/enrich
  tests set `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False`. Keep it.
- **No SQL outside storage.py** (now zero exceptions — `reindex` moved into `Storage.reindex`). The agent/API/ingest
  call the Storage interface — this is the Postgres exit ramp; don't erode it.
- **One `Storage` per connection.** `Storage` serializes its shared sqlite connection with a `threading.Lock`
  (event loop + `run_in_threadpool` workers share one `con`); network embedding stays outside the lock. Two
  `Storage` instances on one connection would each have their own lock — don't.
- **Embedding model is stamped** in `meta` on first ingest; ingesting with a different model is refused until
  `hippo reindex`. `reindex` embeds everything before swapping `chunk_vec`, so a failure leaves the index intact.
- Test dbs must NOT live inside folders that get synced (sqlite WAL files pollute rglob) — use a separate tmp dir
  for the db (see tests/test_ingest.py fixture).
- `Agent(...)` constructions need `defer_model_check=True` or import-time crashes without API keys.
- Chat protocol payloads require `"trigger": "submit-message"` (Vercel AI SubmitMessage schema).
- `chunk_vec` dim is fixed at table creation; changing embedding dim requires `hippo reindex` (drops/recreates table).
- TDD discipline: failing test first; commit per green step.
- **grep uses the `regex` module** with a wall-clock `timeout=` (not stdlib `re`) for ReDoS safety; `re` is still used for FTS tokenization. Pattern length is capped at 200 chars; both violations raise `ValueError`.
- **Tool output is framed as ⟦untrusted document data⟧** — don't strip the delimiters; they are the prompt-injection boundary enforced by the system-prompt "Untrusted content" rule.
- **Retrieval methods take `role` keyword-only with no default** — a forgotten call site must be a TypeError, never an access-control leak. Same for HubDeps.role.

## State (2026-06-13)

v1 + review-hardening merged to main: storage/hybrid search, ingestion (folder sync + upload),
enrichment, agent, API, CLI, React UI, eval harness. PR #2 landed two independent-review passes
(connection lock, safe reindex, embedding-model stamp, citation resolution, etc.). Roadmap items
1+2 (auth/roles/sources) implemented on branch `build/auth-and-sources` (PR pending). Roadmap
item 3 (production-readiness: ingestion limits, grounding enforcement, grep hardening, chunk/drawer
fixes, `hippo backup`, Docker, CI, logging) implemented on branch `build/production-readiness`
(PR #4 merged). Roadmap item 5 (.docx parsing via mammoth, `parse_bytes()` entry point, UI upload
accepts .docx) implemented on branch `build/docx-parsing` (PR pending). Roadmap item 6 (MCP server:
FastMCP at /mcp with bearer-token auth + role filtering, `hippo mcp` stdio command) implemented on
branch `build/mcp-server` (PR pending). Roadmap item 7 (Slack bot: Socket Mode, role-filtered Q&A,
DM+channel @mention, thread-aware history) implemented on branch `build/slack-integration` (PR pending).
190+ tests, eval 4/4 on seed fixtures, UI builds clean.

**Active plan:** see `docs/superpowers/plans/2026-06-12-roadmap.md`. Next: scale (Postgres+pgvector), MCP client/connectors, settings UI, deploy.

**Deferred (spec §12):** Google Drive connector (interface: `list_items()` + `fetch()` -> markdown),
PDF parsing (planned), Postgres+pgvector migration (reimplement Storage), hierarchical summaries, GraphRAG.
