# Embeddings

`embeddings.py` defines how text becomes vectors, and `storage/` enforces that a
database is only ever used with the embedding space it was built for.

## The `Embedder` protocol

A small interface: `embed(texts) -> list[vector]`, plus `.model` (the model name)
and `.dim` (the dimension). Two implementations:

- **`OpenAIEmbedder`** (default, `text-embedding-3-small`) — talks to any
  OpenAI-compatible embeddings endpoint. It is constructed with a `timeout`
  (`HIPPO_EMBED_TIMEOUT_S`, default 60s — the SDK default of 600s is far too long
  for a request in the ingest loop) and a `max_retries` budget
  (`HIPPO_EMBED_MAX_RETRIES`). It **batches** inputs (`EMBED_BATCH = 64`) and
  sends the explicit `dimensions` parameter only for `text-embedding-3-*` models
  (where it's supported).
- **`FakeEmbedder`** — deterministic, offline, no network. Used by the test suite
  and for `HIPPO_EMBEDDING_MODEL=fake`. Retrieval works under it; only chat
  generation needs a real model.

`build_embedder(settings)` selects the implementation from `HIPPO_EMBEDDING_MODEL`
and reads `HIPPO_EMBEDDING_DIM` directly from `Settings` — never from the Config
overlay (embeddings are env-only).

## Where embedding happens

Embedding is always a **network call made outside the storage lock** (in
`upsert_document`, `search_hybrid`, and `reindex`), so a slow embedding batch
never blocks concurrent DB reads. The chunk text stored is **raw**; the
enrichment context line is only prepended to the *embedding input* (see
[RAG pipeline](rag-pipeline.md)).

## The embedding stamp (mixing prevention)

On the first write, `_ensure_embedding_model()` (in `storage/documents.py`)
stamps two `meta` rows: `embedding_model` and `embedding_dim`. Thereafter:

- If the configured model **differs** from the stamp → it raises with a clear
  instruction (`run hippo reindex` or set `HIPPO_EMBEDDING_MODEL=<stamped>`).
  This prevents silently blending two incompatible vector spaces — a same-dim
  model swap followed by `sync` (not `reindex`) would otherwise corrupt retrieval
  quality invisibly.
- If the configured **dimension** differs from the stamp → it raises similarly.
- **Legacy DBs** stamped before dim-tracking: the dim is *not* back-filled from
  the current embedder (the live `chunk_vec` width is unknown there, so a blind
  stamp could record a wrong dim and mask the failure). It's left unstamped; the
  next `hippo reindex` records the true dim.

`chunk_vec`'s dimension is fixed at table creation, so changing the dimension
requires `reindex` (which drops and recreates the table).

## `reindex` safety

`hippo reindex` → `Storage.reindex(embedding_dim)` re-embeds every chunk and
rebuilds `chunk_vec`. It embeds **everything before** destroying the old index
(so a failure mid-run leaves the index intact), does the swap + re-stamp in one
transaction, and aborts if a concurrent ingest changed the `(id, text)` chunk set
during the unlocked embedding window. See [Retrieval → reindex](retrieval.md#reindex).

## Why embeddings are env-only

This is the crux of why `embedding_model`/`embedding_dim` are **not** in
`DB_OVERRIDABLE`: the embedder is built from env *before* the Config overlay
exists, the `chunk_vec` width is fixed at creation, and a reindex reads the
environment. A DB override could neither take effect nor stay accurate, so it
would only create a misleading config display. The env-built embedder is the one
source of truth. See [Config & setup](config-and-setup.md).

## Choosing a model

- **OpenAI:** `text-embedding-3-small` (dim 1536, the default) or
  `text-embedding-3-large`.
- **Ollama (local):** e.g. `nomic-embed-text` (dim 768). Ollama Cloud does not
  serve embeddings — use a local embedding model even when chatting via cloud.
- Always set `HIPPO_EMBEDDING_DIM` to match the model. Decide before first ingest;
  changing later means a `reindex`.
