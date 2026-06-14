# Retrieval

The read path: how Hippo finds the right chunks for a question. All retrieval
lives in `storage/search.py` (the `_SearchMixin`) and is **role-filtered** —
every method takes `role` keyword-only with no default.

## Hybrid search (`search_hybrid`)

The primary retrieval method fuses two independent rankers:

1. **FTS5 BM25** (`_search_fts`) — keyword search over the `chunks_fts` index.
   Query terms are tokenized (`\w+`) and OR-matched; ranked by `bm25()`.
2. **Vector KNN** (`_search_vec`) — semantic search over `chunk_vec` (the
   `sqlite-vec` table), nearest neighbors to the query embedding by distance.

Their rankings are merged with **Reciprocal Rank Fusion (RRF)**:

```
score(chunk) = Σ  1 / (RRF_K + rank_in_list)        RRF_K = 60
```

over both lists, then the top `top_k` (default 8) survivors are returned as
`SearchHit`s. RRF is robust: it doesn't require the two scorers to be on
comparable scales, just to agree on what's good.

### Role filtering, applied to candidates

Both rankers filter to readable folders *in SQL* via `_role_filter(role)` (which
uses `readable_min_roles()` from `roles.py`). So a low-tier caller never even
retrieves chunks from folders above their tier. `_hit()` re-applies the filter
when materializing a hit as defense-in-depth, though candidates are already
filtered.

### The single over-fetch (a perf fix)

`_search_vec` does **one** generous over-fetch — `k = min(limit * VEC_OVERFETCH,
total)` with `VEC_OVERFETCH = 10` — then role-filters the candidates. An earlier
version re-ran the full brute-force KNN in a ×4 backoff loop until enough
survived the filter, which meant several full scans per query for a low-tier user
whose readable folders held a small slice of the corpus. We accept
fewer-than-`limit` results for a role-sparse corpus instead of repeated scans.

## Grep (`grep`)

Exact/regex scan over raw chunk text, role-filtered the same way. Hardening:

- Uses the **`regex` module** with a wall-clock `timeout=` per chunk (not stdlib
  `re`) for ReDoS safety. (`re` is still used only for FTS tokenization.)
- Pattern length is capped at `GREP_MAX_PATTERN` (200 chars); over that raises
  `ValueError`.
- A whole-operation deadline (`GREP_TIMEOUT_S`, 2s) bounds total time, not just
  per-chunk.
- It materializes at most `GREP_MAX_CHUNKS` (5000) role-visible chunks (memory +
  lock-hold bound); if the cap bites it **logs a warning** rather than silently
  truncating.

> These tuning constants live in `storage/search.py` next to the methods that
> read them — that's their single source of truth (tests monkeypatch
> `hippo.storage.search.GREP_TIMEOUT_S`, etc.). They are intentionally not
> re-exported from the package root, where a patched copy would be inert.

## Reindex

`Storage.reindex(embedding_dim)` re-embeds **every** chunk with the current
embedder and rebuilds `chunk_vec`. The safety properties:

- It embeds everything **before** destroying the old index, so a mid-run failure
  (bad key, rate limit, wrong dimension) leaves the existing vectors intact.
- The destroy + repopulate + re-stamp happens in a **single transaction**, only
  after all embeddings succeed.
- It guards against a concurrent ingest during the (unlocked) embedding window by
  snapshotting `(id, text)` of all chunks and **aborting if that set changed**
  before the swap. Comparing full `(id, text)` — not just ids — matters because
  SQLite can reuse freed rowids, so the id set can be unchanged while text
  differs.

Across *processes* (e.g. the `hippo reindex` CLI while `hippo serve` is
ingesting) the two connections have independent locks, so run reindex with no
concurrent sync/upload. See [Embeddings](embeddings.md) and
[CLI](cli.md).

## Backup

`Storage.backup(dest)` writes a consistent single-file snapshot via `VACUUM
INTO` — correct regardless of WAL state, no need to pause writes.

## Why this design

- **Hybrid beats either alone**: BM25 nails exact terms/identifiers; vectors
  catch paraphrase. RRF combines them without scale-matching.
- **Filter in SQL, not after**: cheaper and fail-closed — you can't accidentally
  return a chunk you filtered out in Python.
- **Bounded everything**: grep time, grep memory, KNN fetches, pattern length —
  all capped, because retrieval runs on caller-supplied input.
