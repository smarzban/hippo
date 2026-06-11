# Knowledge Hub — Design Spec

**Date:** 2026-06-11
**Status:** Draft for review

## 1. Overview

An agentic knowledge hub ("team brain"): documents go in (markdown, text, Google Docs exports), and users query the accumulated knowledge through a chat interface. The system answers questions like "how does the polly app integrate with telegram", "why did we do project X", and "what does X solve" — with citations back to the source documents.

**Deployment posture:** personal/local first, architected for team deployment later (shared server, auth, Slack bot).

## 2. Goals

- Ingest `.md`, `.txt`, and HTML (covers Google Docs exports) from synced local folders and chat uploads.
- Answer questions via an agent that actively searches, reads, and follows references across documents — not single-shot RAG.
- Cite every claim with document + section provenance; refuse to answer from general knowledge when the corpus lacks the answer.
- Provider-agnostic LLM and embeddings (swap Claude/GPT/local via config).
- Zero infrastructure for v1: one Python service, one SQLite file, one React UI.

## 3. Non-goals (v1)

- PDF/docx parsing (parser slots in later; pipeline is format-agnostic internally).
- Google Drive API connector (v1 path: export/sync Docs into a watched folder; connector interface is designed in, implementation deferred).
- Slack bot (the API is shaped for it; the bot is a later client).
- Multi-user auth (middleware stub only).
- Corpus-scale map-reduce summarization ("summarize everything" questions hit the tool cap and answer honestly with partial coverage).
- Knowledge graphs / GraphRAG (revisit if query-time agentic digging proves too slow at scale).

## 4. Stack

| Layer | Choice | Rationale |
|---|---|---|
| Agent framework | **Pydantic AI** | Best typed, provider-agnostic Python agent library; DI for tools; structured outputs; `pydantic-evals` for retrieval quality testing |
| API | **FastAPI** | Streams the Vercel AI Data Stream Protocol; Pydantic-native |
| Storage | **SQLite + sqlite-vec + FTS5** | Hybrid search in one embedded file; zero infra; exact search is fast at this corpus scale (≤ ~500K chunks) |
| Frontend | **React chat UI over the Vercel AI protocol** (start from `pydantic/ai-chat-ui`) | Officially supported Pydantic AI integration; streaming, tool-call progress display, upload support |
| Embeddings | Provider-agnostic via config; **default: OpenAI `text-embedding-3-small`** (cheap, ubiquitous), swappable to Voyage / Ollama | Model name stamped per vector; `reindex` command for model swaps |
| Enrichment model | Cheap/small model via config | Contextual lines + document summaries at ingestion |

**Storage exit ramp:** the agent and ingestion code call a storage interface (`search_hybrid`, `get_document`, `add_document`, …), never SQL directly. Team-scale deployment reimplements that interface on Postgres + pgvector + Postgres FTS. SQLite's single-writer limit is the known trigger (multiple concurrent connectors + heavy team usage); WAL mode handles v1's one-writer-many-readers pattern.

## 5. Storage design

One SQLite database file:

- **`documents`** — id, source type (folder / upload / connector), source path/URL, title, content hash, summary, last-synced timestamp.
- **`chunks`** — document FK, text, heading path (`Integrations > Telegram > Webhooks`), position. Powers section-level citations.
- **`chunks_fts`** — FTS5 virtual table over chunk text (BM25 keyword search), trigger-synced.
- **`chunk_embeddings`** — sqlite-vec virtual table; one vector per chunk; embedding model name recorded.

**Hybrid search:** FTS5 and vector search run in parallel; results merged with Reciprocal Rank Fusion. Keyword search covers proper nouns/codenames/acronyms (where embeddings are weak); vectors cover semantic phrasing variance. Every hit carries document + section provenance via SQL join.

## 6. Agent & retrieval design

A Pydantic AI agent in a tool-use loop (max ~15 tool calls per question). Escalation from quick lookup to deep research is emergent — the model decides when one search suffices and when to dig.

**Tools:**

| Tool | Purpose |
|---|---|
| `search(query, top_k)` | Hybrid search; returns chunks with provenance. The workhorse. |
| `read_document(doc_id, section?)` | Full document (or section) fetch. Escalation: read the whole doc instead of guessing from a fragment. |
| `list_documents(source?, query?)` | Browse titles + summaries; discovery for "which docs even discuss project X". |
| `grep(pattern, source?)` | Exact-match scan over raw chunk text. Cheap complement for identifiers/codenames the index mangles. |

**System prompt directives:** answer only from retrieved content; cite every claim (document + section); say "the knowledge base doesn't cover this" rather than improvising; prefer `read_document` over fragment-guessing for "why" questions.

**Multi-turn:** Pydantic AI message history carries conversation context; the agent re-searches only when a follow-up needs new material.

**UX during retrieval:** tool-call events stream to the UI as progress ("searching: project X rationale…", "reading: rfc-014.md"). Deep questions taking 15–30s are acceptable because the work is visible.

**Citations are structural:** chunks carry provenance from storage → agent cites `polly/integrations.md → Telegram` → UI renders source links. This is the core trust feature.

## 7. Ingestion design

One pipeline, multiple entrances. Stages:

1. **Discover & dedupe** — content hash; unchanged files skip; changed files replace their chunks atomically; files removed from a source have their chunks deleted.
2. **Parse** — `.md`, `.txt`, HTML→markdown (covers Google Docs exports). Markdown is the canonical internal format; new formats = new parsers only.
3. **Chunk** — heading-aware splitting, ~500–800 tokens, small overlap, code blocks kept intact, heading path recorded per chunk.
4. **Enrich** (cheap model, config-toggleable) — contextual retrieval line per chunk (prepended before embedding; per Anthropic's published results this materially cuts retrieval failures) + one-paragraph document summary for `list_documents`.
5. **Embed & index** — batch embed; chunks + vectors + FTS written in one transaction per document.

**Entrances:**

- **CLI:** `hub sync <folder>` (register + sync), `hub sync` (re-sync all sources), `hub add <file>`, `hub sync --watch` (filesystem events), `hub reindex` (re-embed after model swap).
- **Chat upload:** UI drag-and-drop → `POST /ingest` → same pipeline → confirmation in chat ("added polly-runbook.md — 14 chunks").
- **Connector interface (future):** `list_items()` + `fetch(item) → (markdown, metadata)`. Folder source is implementation #1; Google Drive is #2; same pipeline regardless.

**Failure behavior:** per-file isolation — one bad file logs and the run continues; sync ends with a report (synced / skipped-unchanged / failed). Ingestion runs as a separate process from chat, sharing only the database (WAL mode).

## 8. API surface

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Agent conversation; streams Vercel AI Data Stream Protocol (text deltas, tool events, citations) |
| `POST /ingest` | File upload into the ingestion pipeline |
| `GET /documents`, `GET /documents/{id}` | Browse indexed documents |
| `GET /sources`, `POST /sources` | List / register sync sources |
| `GET /health` | Liveness |

Auth: none in v1; all routes pass through an auth middleware stub so team deployment implements one function, not a retrofit. Slack bot later is another client of `POST /chat`.

## 9. Error handling

- **Agent:** tool-call cap (~15) prevents runaway loops; on cap, answer with what was found and state coverage limits. LLM provider errors surface to the UI as retryable messages; provider swap is config.
- **Ingestion:** per-file isolation; failures reported, never fatal to the run; chat unaffected by ingestion failures (separate processes).
- **Storage:** single transaction per document (no half-indexed docs); WAL mode for concurrent read/write.
- **Honesty over coverage:** corpus gaps produce "not covered" answers, never improvisation.

## 10. Testing

1. **Unit** — chunker (heading splits, code-block integrity), content hashing, RRF merge. Deterministic, fast.
2. **Storage integration** — storage interface against temp SQLite: index → search → update → delete; FTS and vectors stay consistent.
3. **Agent behavior, no API calls** — Pydantic AI `TestModel` / `FunctionModel`: assert tool wiring, citation extraction, tool-call cap.
4. **Retrieval quality eval** — golden set (~20 real question → expected-source-document pairs) via `pydantic-evals`; the regression gate for chunking/embedding changes.

## 11. Build order (suggested phasing for the implementation plan)

1. Storage layer + schema + hybrid search (testable standalone)
2. Ingestion pipeline + CLI (`hub sync` a real folder; inspect the index)
3. Agent + tools + FastAPI streaming endpoint (chat via curl)
4. React chat UI (Vercel AI protocol; uploads)
5. Enrichment (contextual lines + summaries) + retrieval eval golden set
6. Polish: `--watch` mode, sync reports, document browse endpoints

## 12. Future (explicitly deferred)

- Google Drive connector; Confluence/Jira connectors
- Slack bot client
- PDF/docx parsers
- Postgres + pgvector migration (team scale)
- Real auth (replace middleware stub)
- Document-summary tree / hierarchical retrieval for broad survey questions
- GraphRAG, if "why" questions at scale outgrow query-time digging
