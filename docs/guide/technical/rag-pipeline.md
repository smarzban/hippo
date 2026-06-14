# The RAG pipeline (ingestion)

How a file becomes searchable, cited knowledge. The write path:

```
parse → hash-dedup → chunk → enrich → embed → index   (one transaction per document)
```

Implemented by `Ingestor` in `ingest.py`, with `parsers.py`, `chunking.py`,
`enrich.py`, `embeddings.py`, and `storage/` doing the work.

## 1. Parse (`parsers.py`)

`parse_bytes(filename, data)` is the canonical bytes entry point (used by
`/ingest`). The `SUPPORTED` set is the gate: `.md`, `.txt`, `.html`/`.htm`,
`.docx`. Each parser returns `(title, canonical_markdown)`:

- **Markdown / text** — taken largely as-is.
- **HTML** — converted to Markdown.
- **`.docx`** — via `mammoth` (docx → HTML → Markdown), preserving heading
  styles. A decompressed-size guard (`HIPPO_MAX_DECOMPRESSED_BYTES`) defends
  against ZIP bombs.

Everything downstream works on canonical Markdown, so the rest of the pipeline is
format-agnostic.

## 2. Dedup by content hash

Each document has a `content_hash`. On (re-)ingest, `Storage.is_unchanged(path,
hash)` skips work when nothing changed; an upsert replaces the document and all
its chunks atomically when it has. This is what makes re-uploading a new version,
or re-syncing a folder, cheap and idempotent.

## 3. Chunk (`chunking.py`)

`chunk_markdown()` is **heading-aware**: it splits along Markdown headings so a
chunk carries a meaningful `heading_path` (used in citations as the `> section`).
Key properties:

- Targets ~750 tokens per chunk (`HIPPO_CHUNK_MAX_CHARS`, ~3000 chars).
- **Code fences are atomic** — a fenced block is never split mid-fence.
- An overlap tail (`HIPPO_CHUNK_OVERLAP_CHARS`) is prepended to the next chunk for
  continuity, and re-checked against `max_chars` before prepending so overlap
  can't push a chunk over the limit.

Good chunk boundaries matter: they're the unit of retrieval and the granularity
of a citation.

## 4. Enrich (`enrich.py`, optional)

When `HIPPO_ENRICH_ENABLED` is true, an `Enricher` (using the cheap
`HIPPO_ENRICH_MODEL`) produces:

- a **document summary**, and
- a **contextual line** per chunk (what this chunk is about, in the document's
  context).

The contextual line is **prepended to the embedding input** (`context + "\n" +
chunk`) so retrieval benefits from it, but the **stored chunk text stays raw** —
citations show the real content, not the synthetic context. Enrichment is
**best-effort**: if the model returns empty content (a known quirk of some local
models) or errors, enrichment yields `""` rather than failing the ingest.

## 5. Embed (`embeddings.py`)

The enriched inputs are embedded by the configured `Embedder`
(`OpenAIEmbedder` by default; `FakeEmbedder` offline). Embedding is a **network
call made outside the storage lock** so a slow batch can't block concurrent
reads. Inputs are batched, and for `text-embedding-3-*` the explicit `dimensions`
parameter is sent. See [Embeddings](embeddings.md).

## 6. Index (`storage/`)

`Storage.upsert_document(...)` writes the document, its chunks, and their vectors
in **one transaction per document** (per-file isolation: one bad file doesn't
abort the batch). It writes to:

- `documents` (path, title, content, hash, summary, `folder_id`),
- `chunks` (position, `heading_path`, raw text) — mirrored into an **FTS5** index
  by triggers,
- `chunk_vec` (a `sqlite-vec` virtual table) holding the embedding per chunk.

Before the first write it calls `_ensure_embedding_model()` to **stamp** the
embedding model + dimension and refuse to mix vector spaces — see
[Embeddings](embeddings.md).

## Two ways content arrives

- **Upload** (`POST /ingest`): one document per destination folder, write-gated
  by `can_write(role, folder.min_role, folder.origin)`; size-checked against
  `HIPPO_MAX_UPLOAD_BYTES` (413) and `HIPPO_MAX_DOC_CHARS` (skipped).
- **Folder sync** (`sync_folder` in `ingest.py`): mounts a filesystem directory
  as a pull-only (`origin='folder'`) node, ingests every supported file, and on
  re-sync handles **deletions** (prunes docs whose files vanished) and filters
  noise via `IGNORED_EXTENSIONS`/`IGNORED_DIRS`. The mount location must be inside
  the `HIPPO_SOURCE_ROOTS` allowlist, re-checked on every sync.

## Failure isolation

Each document is its own transaction, so a parse/embed failure on one file is
reported (`status: failed`/`skipped`) without rolling back the others. The
`reindex` path is even more careful — it embeds everything before swapping the
vector table, so a mid-run failure leaves the existing index intact (see
[Retrieval → reindex](retrieval.md#reindex)).
