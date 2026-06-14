# Architecture

Hippo is a retrieval-augmented generation (RAG) system with access control built
in at the retrieval layer. This page is the map; the rest of the technical docs
zoom in.

## The big picture

```
                 ┌─────────────────────────────────────────────┐
   ingest ──────▶│  documents → chunks → embeddings (chunk_vec) │   one SQLite file
   (sync/upload) │  + FTS5 index + folders/users/tokens/config  │   (HIPPO_DB_PATH)
                 └───────────────▲─────────────────────────────┘
                                 │  Storage  (all SQL, one lock, role-filtered)
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                         │
     /chat (web)              /mcp  (MCP)               Slack bot
   pydantic-ai agent       same 4 tools             same agent
        │                        │                         │
        └──── 4 tools: search / read_document / list_documents / grep ────┘
                                 │
                         model provider (OpenAI-compatible)
```

Three query surfaces — web chat, MCP, Slack — all run the **same agent** over the
**same four tools**, and all retrieval goes through the **same role-filtered
`Storage`**. There is no second code path that could leak content the caller
can't see.

## Two halves: ingestion and querying

**Ingestion** (write path): documents arrive by upload or filesystem sync, are
parsed to canonical Markdown, split into heading-aware chunks, optionally
enriched with context, embedded, and indexed into SQLite (a vector table plus a
full-text index). See [RAG pipeline](rag-pipeline.md).

**Querying** (read path): a question goes to the pydantic-ai agent, which calls
tools that hit hybrid retrieval (keyword + vector, fused), all filtered by the
caller's role. The agent composes a grounded, cited answer. See
[Retrieval](retrieval.md) and [Agent](agent.md).

## Data flow of a chat request

1. `POST /chat` — `verify_request` authenticates the caller and resolves their
   role (see [Auth & RBAC](auth-and-rbac.md)).
2. A `HubDeps(store, role)` is built and the request is streamed through
   pydantic-ai's Vercel-AI adapter using the live agent.
3. The agent calls tools (`search`/`read_document`/`list_documents`/`grep`).
   Each passes `role` into `Storage`, which filters by folder tier.
4. Tool output is wrapped in the `⟦untrusted document data⟧…⟦end⟧` boundary.
5. The model answers, citing `[path > section]`. A server-side validator logs if
   a substantial answer lacks a citation.

## Storage: one file, one lock, all SQL

Everything persists in one SQLite database (with WAL, the `sqlite-vec` extension
for vector KNN, and FTS5 for keyword search). **All SQL lives in the `storage/`
package** behind a `Storage` facade. A single shared connection is serialized
with one `threading.Lock` because the event loop and `run_in_threadpool` workers
share it; network embedding always happens *outside* the lock. This is also the
**Postgres exit ramp**: swap the `Storage` implementation and the rest of the app
is unchanged. See [Storage layer](storage-layer.md).

## Access control at the data layer

Roles are `user` < `admin` < `owner`, defined once in `roles.py`. Documents live
in a **folder tree**; each folder has a tier; retrieval methods take `role`
keyword-only (no default) and filter to readable folders via
`readable_min_roles()`. Because filtering is in `Storage`, every surface inherits
it. See [Auth & RBAC](auth-and-rbac.md).

## The API as a thin assembler

`build_app` (in `api/app.py`) wires everything: it builds an `AppContext`
(connection, store, config overlay, agent cache, ingestor), creates the FastAPI
app, mounts session middleware, registers the route modules
(session/account/content/admin), mounts the MCP app, and serves the SPA last. The
route handlers close over the shared context and read effective config **live**
per request. See [API layer](api-layer.md).

## Configuration model

`Settings` reads env (`HIPPO_` prefix). A `Config` overlay lets owners change a
small set of operational keys at runtime (stored in a `config` table; DB wins for
those keys). **Secrets and the embedding model/dim are env-only** and never in
the DB. See [Config & setup](config-and-setup.md).

## Design principles

- **Grounding over fluency.** The whole value proposition is "answers only from
  your docs, with citations." The agent prompt, the untrusted boundary, and the
  grounding validator all serve this.
- **Access control that can't be bypassed.** Enforce it once, at the data layer,
  with a fail-closed signature (`role` keyword-only, no default).
- **One source of truth per concept.** Rank lives only in `roles.py`; SQL lives
  only in `storage/`; secrets live only in env.
- **Simple to self-host.** One file, no external services; runs offline with fake
  embeddings.
- **Fail safe, fail loud.** A legacy DB is rejected on startup; an embedding-model
  mismatch refuses to ingest; misconfigured auth modes raise at construction.

## Where to go next

The [module map](README.md#module-map-srchippo) lists every module; follow the
reading order there to go deeper.
